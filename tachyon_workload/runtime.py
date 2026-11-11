"""Bounded RPC/CLI adapters and a durable, private operation journal."""

import asyncio
import base64
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import tomllib
import urllib.request
import uuid

from .planner import FEE
from .faucet import validate_config as validate_faucet_config


def emit(event, **fields):
    # Never log CLI stdout: receipt exports and wallet internals are private.
    print(json.dumps({"event": event, **fields}), flush=True)


def private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode) or mode & 0o077:
        raise ValueError(f"directory must be real and mode 0700: {path}")
    return path


def private_file(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    mode = os.fstat(fd).st_mode
    if not stat.S_ISREG(mode) or mode & 0o077:
        os.close(fd)
        raise ValueError(f"file must be regular and mode 0600: {path}")
    return os.fdopen(fd, "a+")


def load_config(path):
    path = Path(path).absolute()
    with path.open("rb") as file:
        config = tomllib.load(file)
    defaults = dict(wallet_count=32, max_transactions=8, concurrency=4,
                    activation_height=8, poll_seconds=2, command_timeout=1800,
                    allow_external_transactions=False,
                    timing=False,
                    min_amount_zat=100_000, max_amount_zat=2_000_000,
                    seed_amount_zat=10_000_000, seed_notes=3,
                    weights={"send": 80, "unshield": 8, "shield": 8,
                             "transparent-send": 4})
    allowed = {*defaults, "rpc_url", "oss_url", "devtool", "wallet_root", "state_dir", "cookie_file",
               "oss_cookie_file", "faucet"}
    if set(config) - allowed:
        raise ValueError(f"unknown config fields: {sorted(set(config) - allowed)}")
    config = defaults | config
    if type(config["allow_external_transactions"]) is not bool:
        raise ValueError("allow_external_transactions must be true or false")
    if type(config["timing"]) is not bool:
        raise ValueError("timing must be true or false")
    for key in ("devtool", "wallet_root", "state_dir"):
        config[key] = (path.parent / config[key]).absolute()
    for key in ("cookie_file", "oss_cookie_file"):
        if config.get(key):
            config[key] = (path.parent / config[key]).absolute()
    for key in ("rpc_url", "oss_url"):
        url = urllib.parse.urlsplit(str(config.get(key, "")))
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError(f"{key} must be an HTTP(S) URL without embedded credentials")
    for name, minimum, maximum in (
        ("wallet_count", 2, 128), ("max_transactions", 0, 128),
        ("concurrency", 1, 16), ("activation_height", 1, 2**32 - 1),
        ("poll_seconds", 1, 60), ("command_timeout", 30, 7200),
        ("min_amount_zat", 1, 10**12), ("max_amount_zat", 1, 10**12),
        ("seed_amount_zat", 1, 10**12), ("seed_notes", 1, 16),
    ):
        value = config[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    if config["min_amount_zat"] > config["max_amount_zat"]:
        raise ValueError("min_amount_zat exceeds max_amount_zat")
    if config["seed_amount_zat"] < config["min_amount_zat"] + FEE:
        raise ValueError("seed_amount_zat must cover the minimum payment plus fee")
    weights = config["weights"]
    if (set(weights) != set(defaults["weights"]) or
            any(type(w) is not int or w < 0 for w in weights.values()) or not sum(weights.values())):
        raise ValueError("weights must contain four payment kinds with nonnegative integer weights")
    if config["max_transactions"] > config["wallet_count"] + 1:
        raise ValueError("cap exceeds one transaction per simulated wallet plus the treasury")
    if "faucet" in config:
        config["faucet"] = validate_faucet_config(config["faucet"])
    return config


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("RPC redirects are disabled")


class Rpc:
    def __init__(self, config):
        self.config = config
        self.opener = urllib.request.build_opener(NoRedirect())

    async def call(self, method, params=None):
        def request():
            headers = {"Content-Type": "application/json"}
            if self.config.get("cookie_file"):
                cookie = self.config["cookie_file"].read_text().strip()
                headers["Authorization"] = "Basic " + base64.b64encode(cookie.encode()).decode()
            data = json.dumps(dict(jsonrpc="2.0", id=1, method=method, params=params or [])).encode()
            req = urllib.request.Request(self.config["rpc_url"], data, headers)
            with self.opener.open(req, timeout=30) as response:
                payload = response.read(16 * 1024 * 1024 + 1)
            if len(payload) > 16 * 1024 * 1024:
                raise RuntimeError("RPC response exceeds 16 MiB")
            envelope = json.loads(payload)
            if envelope.get("id") != 1 or "result" not in envelope or envelope.get("error"):
                raise RuntimeError(f"{method}: {envelope.get('error', 'invalid RPC envelope')}")
            return envelope["result"]
        return await asyncio.to_thread(request)


class CliError(RuntimeError):
    pass


class Devtool:
    def __init__(self, config):
        self.config = config
        self.slots = asyncio.Semaphore(config["concurrency"])
        self.locks = {}
        self.prewarm_slots = asyncio.Semaphore(config["concurrency"])

    async def call(self, wallet, *args):
        lock = self.locks.setdefault(wallet, asyncio.Lock())
        async with lock, self.slots:
            return await self._run(wallet, args)

    async def prewarm(self, wallet):
        # The command only reads wallet history; its long proof computation
        # must not hold a wallet command slot needed by journal recovery.
        async with self.prewarm_slots:
            return await self._run(wallet, ("prewarm-epochs",))

    async def _run(self, wallet, args):
        env = {**os.environ, "TACHYON_OSS_URL": self.config["oss_url"]}
        if self.config.get("oss_cookie_file"):
            env["TACHYON_OSS_COOKIE"] = str(self.config["oss_cookie_file"])
        if self.config["timing"]:
            env["TACHYON_TIMING"] = "1"
        process = await asyncio.create_subprocess_exec(
            str(self.config["devtool"]), "tachyon", "--wallet-dir",
            str(self.config["wallet_root"] / wallet), *map(str, args),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), self.config["command_timeout"])
        except BaseException:
            # Do not leave a writer running after an ambiguous timeout/cancellation.
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        self.log_timing(stderr.decode(errors="replace"), wallet, args)
        if process.returncode:
            raise CliError(stderr.decode(errors="replace").strip()[-2000:])
        return json.loads(stdout)

    @staticmethod
    def log_timing(stderr, wallet, args):
        for line in stderr.splitlines():
            if not line.startswith("TACHYON_TIMING "):
                continue
            try:
                timing = json.loads(line[len("TACHYON_TIMING "):])
                emit("timing", wallet=wallet,
                     command=str(args[0]) if args else None, **timing)
            except (ValueError, TypeError):
                continue

    async def sync(self, wallet):
        reorg = False
        for _ in range(100):
            result = await self.call(wallet, "sync", "--max-blocks", 10000)
            reorg |= result.get("reorg", False)
            if result["caught_up"]:
                return {**result, "reorg": reorg}
        raise RuntimeError(f"{wallet}: sync exceeded 1,000,000 blocks; rerun to continue")


class Journal:
    def __init__(self, path):
        self.path = private_dir(path)
        self.lock = private_file(self.path / "sidecar.lock")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.lock.close()
            raise RuntimeError("another sidecar owns this state directory") from error
        with private_file(self.path / "workload.sqlite"):
            pass
        self.db = sqlite3.connect(self.path / "workload.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY, tip TEXT NOT NULL, height INTEGER NOT NULL,
                mode TEXT NOT NULL, target INTEGER NOT NULL, finished INTEGER NOT NULL DEFAULT 0,
                mined_hash TEXT, mining_requested INTEGER NOT NULL DEFAULT 0, UNIQUE(tip, mode));
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, round_id INTEGER NOT NULL REFERENCES rounds(id),
                wallet TEXT NOT NULL, recipient TEXT, args TEXT NOT NULL, kind TEXT NOT NULL,
                txid TEXT, status TEXT NOT NULL DEFAULT 'planned',
                receipt_delivered INTEGER NOT NULL DEFAULT 0);
        """)

        if "expected" not in {row[1] for row in self.db.execute("PRAGMA table_info(rounds)")}:
            self.db.execute("ALTER TABLE rounds ADD COLUMN expected TEXT NOT NULL DEFAULT '[]'")
            self.db.commit()

    def close(self):
        self.db.close()
        self.lock.close()

    def pin(self, key, value):
        encoded = json.dumps(value, sort_keys=True)
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row and row[0] != encoded:
            raise RuntimeError(f"{key} changed; use a separate state directory, never reuse keys across regenesis")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, encoded))

    def require_reconcile(self, wallet):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?, 'true')", ("reconcile:" + wallet,))

    def needs_reconcile(self, wallet):
        return self.db.execute("SELECT 1 FROM metadata WHERE key=?", ("reconcile:" + wallet,)).fetchone() is not None

    def reconciled(self, wallet):
        with self.db:
            self.db.execute("DELETE FROM metadata WHERE key=?", ("reconcile:" + wallet,))

    def defer(self, round_id):
        with self.db:
            self.db.execute("UPDATE rounds SET mode='deferred' WHERE id=? AND mining_requested=0", (round_id,))

    def resume_deferred(self, tip, height, mode):
        deferred = [r[0] for r in self.db.execute("SELECT id FROM rounds WHERE mode='deferred' AND finished=0")]
        if not deferred:
            return
        # Reconcile first: operations mined during prewarm keep their original
        # IDs and are not requested again. Remaining operations share a new tip.
        with self.db:
            self.db.execute("INSERT INTO rounds(tip,height,mode,target) VALUES(?,?,?,0)", (tip,height,mode))
            round_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            for previous in deferred:
                self.db.execute("UPDATE operations SET round_id=? WHERE round_id=? AND status NOT IN ('mined','expired')",
                                (round_id,previous))
                self.db.execute("UPDATE rounds SET finished=1 WHERE id=?", (previous,))

    def new_round(self, tip, height, mode, target, expected=None):
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO rounds(tip,height,mode,target,expected) VALUES (?,?,?,?,?)",
                (tip, height, mode, target, json.dumps(expected or [])))
        if not cursor.rowcount:
            return None
        return cursor.lastrowid

    def plan(self, round_id, wallet, recipient, args, *, faucet=None):
        operation = "workload:" + uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO operations(id,round_id,wallet,recipient,args,kind) VALUES (?,?,?,?,?,?)",
                (operation, round_id, wallet, recipient, json.dumps(args), args[0]))
            if faucet is not None:
                policy, amount, purpose = faucet
                row = self.db.execute("SELECT address FROM faucet_policies WHERE id=?", (policy,)).fetchone()
                if (wallet != "treasury" or recipient is not None or
                        row is None or type(amount) is not int or amount < 0 or
                        purpose not in ("payout", "replenish") or
                        (purpose == "payout" and (amount == 0 or args !=
                            ["unshield", "--to", row["address"], "--amount-zat", str(amount)])) or
                        (purpose == "replenish" and (args[0] != "shield" or amount != 0))):
                    raise ValueError("faucet accounting must match the journaled treasury payment")
                self.db.execute("INSERT INTO faucet_operations VALUES(?,?,?,?)",
                                (operation, policy, amount, purpose))
        return self.get(operation)

    def get(self, operation):
        return dict(self.db.execute("SELECT * FROM operations WHERE id=?", (operation,)).fetchone())

    def update(self, operation, **fields):
        if set(fields) - {"txid", "status", "receipt_delivered"}:
            raise ValueError("unknown operation field")
        with self.db:
            self.db.execute("UPDATE operations SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                            (*fields.values(), operation))

    def unfinished(self):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM operations WHERE status IN ('planned','pending') OR "
            "(recipient IS NOT NULL AND receipt_delivered=0 AND txid IS NOT NULL)")]

    def finish(self, round_id, mined_hash=None):
        with self.db:
            self.db.execute("UPDATE rounds SET finished=1,mined_hash=? WHERE id=?", (mined_hash, round_id))

    def request_mining(self, round_id):
        with self.db:
            self.db.execute("UPDATE rounds SET mining_requested=1 WHERE id=?", (round_id,))

    def active_rounds(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM rounds WHERE finished=0 AND mode != 'deferred' ORDER BY id")]

    def round_operations(self, round_id):
        return [dict(row) for row in self.db.execute("SELECT * FROM operations WHERE round_id=?", (round_id,))]

    def reconcile(self, wallet, transactions):
        # Revisit mined records too: devtool can change them back to pending after a reorg.
        with self.db:
            for tx in transactions:
                if tx["operation_id"]:
                    self.db.execute("UPDATE operations SET status=?,txid=? WHERE wallet=? AND id=?",
                                    (tx["status"], tx["txid"], wallet, tx["operation_id"]))
