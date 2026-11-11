"""Sync wallets, deliver confidential receipts, and schedule bounded rounds."""

import asyncio
import hashlib
import json
import os
import random

from .planner import choose_payment, funding_payment
from .faucet import Faucet
from .runtime import CliError, Devtool, Journal, Rpc, emit, private_dir


class Runner:
    def __init__(self, config, seed=None):
        self.config = config
        self.journal = Journal(config["state_dir"])
        self.faucet = Faucet(self.journal, config.get("faucet"))
        self.receipts = private_dir(config["state_dir"] / "receipts")
        self.rpc = Rpc(config)
        self.cli = Devtool(config)
        self.rng = random.Random(seed)
        self.wallets = ["treasury"] + [f"sim-{i:03}" for i in range(1, config["wallet_count"] + 1)]
        # A previous process may have committed a wallet reorg just before it
        # crashed. Reconcile mined operations too on the first snapshot.
        for wallet in self.wallets:
            self.journal.require_reconcile(wallet)
        self.addresses = {}
        self.wallet_tips = {}
        self.prewarm_task = None
        self.prewarm_key = None
        self.prewarm_ready = None
        self.prewarm_token = None
        self.journal.pin("wallet_root", str(config["wallet_root"]))

    async def network(self):
        info = await self.rpc.call("getblockchaininfo")
        if info["chain"] not in ("test", "regtest") or info.get("pruned", True):
            raise RuntimeError("workload requires a trusted private test/regtest archive node")
        upgrades = list(info.get("upgrades", {}).values())
        if not any(u["name"] == "NuTachyon" and u["status"] == "active" and
                   u["activationheight"] == self.config["activation_height"] for u in upgrades):
            raise RuntimeError("NuTachyon must be active at the configured height")
        genesis = await self.rpc.call("getblockhash", [0])
        activation = await self.rpc.call("getblockhash", [self.config["activation_height"]])
        identity = hashlib.sha256(bytes.fromhex(genesis) + self.config["activation_height"].to_bytes(4, "little") +
                                  bytes.fromhex(activation)).hexdigest()
        self.journal.pin("network_id", identity)
        return info, identity

    async def initialize(self, create=False):
        info, identity = await self.network()
        private_dir(self.config["wallet_root"])
        for wallet in self.wallets:
            directory = self.config["wallet_root"] / wallet
            if not (directory / "tachyon.sqlite").exists():
                if not create:
                    raise RuntimeError(f"{wallet} is missing; run init first")
                args = ["init", "--rpc-url", self.config["rpc_url"], "--activation-height",
                        self.config["activation_height"], "--accept-mock-proofs"]
                if self.config.get("cookie_file"):
                    args += ["--cookie-file", str(self.config["cookie_file"])]
                await self.cli.call(wallet, *args)
            private_dir(directory)
            address = await self.cli.call(wallet, "address")
            if address["network_id"] != identity:
                raise RuntimeError(f"{wallet} belongs to a different chain; refusing to spend")
            self.journal.pin("address:" + wallet, address["funding_address"])
            self.addresses[wallet] = address
        self.faucet.initialize(info["blocks"], info["bestblockhash"], self.addresses)
        emit("ready", wallets=len(self.wallets), miner_address=self.addresses["treasury"]["funding_address"],
             network_id=identity)

    async def snapshot(self):
        unfinished = self.journal.unfinished()
        async def wallet_snapshot(wallet):
            synced = await self.cli.sync(wallet)
            balance = await self.cli.call(wallet, "balance-summary")
            if synced.get("reorg") or self.journal.needs_reconcile(wallet):
                transactions = await self.cli.call(wallet, "transactions")
            else:
                ids = [operation["id"] for operation in unfinished if operation["wallet"] == wallet]
                if ids:
                    args = ["transaction-status"] + [arg for operation in ids for arg in ("--id", operation)]
                    transactions = await self.cli.call(wallet, *args)
                else:
                    transactions = {"transactions": []}
            self.journal.reconcile(wallet, transactions["transactions"])
            self.journal.reconciled(wallet)
            self.wallet_tips[wallet] = (synced["height"], synced["hash"])
            emit("synced", wallet=wallet, height=synced["height"], tachyon_zat=balance["available_zat"],
                 transparent_available_zat=(balance.get("transparent") or {}).get("available_zat"))
            return wallet, balance
        return dict(await asyncio.gather(*(wallet_snapshot(wallet) for wallet in self.wallets)))

    async def sync_faucet(self):
        if not self.faucet.tracked:
            return True
        caught_up = await self.faucet.sync(self.rpc, *self.wallet_tips["treasury"])
        emit("faucet", **self.faucet.status())
        return caught_up

    async def funding_utxos(self, busy, balances):
        # Devtool already validated ownership, maturity, reservations, and the
        # canonical funding snapshot; selected inputs are checked again at signing.
        result = {}
        for wallet in self.wallets:
            if wallet in busy:
                continue
            transparent = balances[wallet].get("transparent") or {}
            result[wallet] = list(transparent.get("funding_candidates") or [])
        return result

    async def lookup(self, operation):
        try:
            return await self.cli.call(operation["wallet"], "operation", operation["id"])
        except CliError as error:
            if "unknown operation ID" in str(error):
                return None
            raise

    async def deliver(self, operation):
        if not operation["recipient"] or operation["receipt_delivered"]:
            return
        receipt = self.receipts / (operation["id"].replace(":", "-") + ".json")
        temporary = receipt.with_suffix(".partial")
        if not receipt.exists():
            # Interrupted CLI export may leave a partial file. Only remove our
            # own staging file; the committed receipt and wallet DB are retained.
            if temporary.exists():
                temporary.unlink()
            await self.cli.call(operation["wallet"], "export-receipt", "--txid", operation["txid"],
                                "--out", str(temporary))
            os.replace(temporary, receipt)
            fd = os.open(self.receipts, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        # receive is idempotent, including if a crash happened before journal update.
        await self.cli.call(operation["recipient"], "receive", "--receipt", str(receipt))
        self.journal.update(operation["id"], receipt_delivered=1)

    async def execute(self, operation):
        saved = await self.lookup(operation)
        if saved is None:
            try:
                await self.cli.call(operation["wallet"], *json.loads(operation["args"]),
                                    "--operation-id", operation["id"],
                                    "--expected-tip", self.wallet_tips[operation["wallet"]][1])
            except (CliError, TimeoutError):
                saved = await self.lookup(operation)
                if saved is None:
                    # Keep the request and its ID; never make a blind replacement.
                    raise
            else:
                saved = await self.lookup(operation)
        if saved is None:
            raise RuntimeError("payment returned without a durable operation")
        self.journal.update(operation["id"], txid=saved["txid"], status=saved["status"])
        await self.deliver(self.journal.get(operation["id"]))
        if saved["status"] == "pending":
            mempool = await self.rpc.call("getrawmempool")
            if saved["txid"] not in mempool:
                # submit re-syncs, recognizes mined transactions, and rejects expiry.
                await self.cli.call(operation["wallet"], "submit", "--txid", saved["txid"])
        emit("payment", wallet=operation["wallet"], kind=operation["kind"], txid=saved["txid"],
             operation_id=operation["id"], status=saved["status"])
        faucet = self.journal.db.execute("SELECT * FROM faucet_operations WHERE operation_id=?",
                                        (operation["id"],)).fetchone()
        if faucet:
            emit("faucet_payment", purpose=faucet["purpose"], policy_id=faucet["policy_id"],
                 amount_zat=faucet["amount_zat"], txid=saved["txid"], status=saved["status"],
                 operation_id=operation["id"])

    async def recover(self):
        for operation in self.journal.unfinished():
            if operation["wallet"] not in self.wallets or (operation["recipient"] and operation["recipient"] not in self.wallets):
                raise RuntimeError("pending operation needs an excluded wallet; restore wallet_count")
            await self.execute(operation)

    def matches_round(self, transactions, expected):
        actual = set(transactions)
        # Shared testnets can include faucet/user traffic, but every payment
        # promised by this round must still be present.
        return expected <= actual if self.config["allow_external_transactions"] else expected == actual

    async def mine_round(self, round_record):
        round_record = next(row for row in self.journal.active_rounds() if row["id"] == round_record["id"])
        tip = await self.rpc.call("getbestblockhash")
        operations = self.journal.round_operations(round_record["id"])
        expected = (set(json.loads(round_record["expected"])) if round_record["mode"] == "prewarm"
                    else {op["txid"] for op in operations if op["status"] != "expired"})
        if None in expected:
            raise RuntimeError("round contains an unresolved operation; retry with the same journal")
        if tip != round_record["tip"]:
            # A prior generate may have succeeded despite a timeout. Inspect its
            # one successor rather than ever blindly calling generate again.
            successor = await self.rpc.call("getblock", [str(round_record["height"] + 1), 1])
            if (successor["previousblockhash"] != round_record["tip"] or
                    not self.matches_round(successor["tx"][1:], expected)):
                raise RuntimeError("chain advanced outside this round; competing miner or reorg; inspect before resuming")
            self.journal.finish(round_record["id"], successor["hash"])
            emit("mined_recovered", height=successor["height"], transactions=len(expected),
                 external_transactions=len(set(successor["tx"][1:]) - expected))
            return
        if not self.matches_round(await self.rpc.call("getrawmempool"), expected):
            raise RuntimeError("mempool differs from this round; refusing to mine an inaccurate batch")
        if round_record["mining_requested"]:
            raise RuntimeError("generate was already requested but its successor is not visible; "
                               "outcome remains ambiguous, refusing to request another block")
        # Persisted round remains unfinished on timeout, so restart inspects first.
        self.journal.request_mining(round_record["id"])
        hashes = await self.rpc.call("generate", [1])
        block = await self.rpc.call("getblock", [hashes[0], 1])
        if block["previousblockhash"] != round_record["tip"] or not self.matches_round(block["tx"][1:], expected):
            raise RuntimeError("mined block did not match this round; check competing miners/template policy")
        self.journal.finish(round_record["id"], block["hash"])
        emit("mined", height=block["height"], transactions=len(expected), target=round_record["target"], hash=block["hash"],
             external_transactions=len(set(block["tx"][1:]) - expected))

    async def prewarm(self):
        async def sync(wallet):
            result = await self.cli.sync(wallet)
            if result.get("reorg"):
                self.journal.require_reconcile(wallet)
        async def phase(coroutines):
            tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
            try:
                return await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        # Finish reorg invalidations in every wallet before any builder acquires
        # a lease. Mining continues independently through both phases.
        await phase(sync(wallet) for wallet in self.wallets)
        return await phase(self.cli.prewarm(wallet) for wallet in self.wallets)

    @staticmethod
    def evidence_token(status):
        return status["namespace"], status["generation"]

    async def evidence_ready(self, info, mode):
        feed = await self.rpc.call("gettachyonblock", [str(info["blocks"])])
        epoch = feed["poolHeight"] // feed["epochLength"]
        if epoch == 0:
            key = None
        else:
            entry = self.config["activation_height"] + epoch * feed["epochLength"]
            key = (epoch, await self.rpc.call("getblockhash", [entry]))
        if self.prewarm_task:
            job_epoch, job_hash = self.prewarm_key
            job_entry = self.config["activation_height"] + job_epoch * feed["epochLength"]
            canonical = epoch >= job_epoch and await self.rpc.call("getblockhash", [job_entry]) == job_hash
            if not canonical:
                self.prewarm_task.cancel()
                await asyncio.gather(self.prewarm_task, return_exceptions=True)
                self.prewarm_task = None
                self.prewarm_ready = None
            elif self.prewarm_task.done():
                try:
                    results = self.prewarm_task.result()
                except CliError as error:
                    status = await self.cli.call("treasury", "evidence-generation")
                    if "invalidated" not in str(error) or self.evidence_token(status) == self.prewarm_token:
                        raise  # Unknown/proof failures are never retried blindly.
                    self.prewarm_task = None
                    self.prewarm_ready = None
                    emit("epoch_prewarm", state="invalidated", epoch=job_epoch - 1)
                else:
                    if any(result["through_epoch"] is None or result["through_epoch"] < job_epoch - 1 for result in results):
                        raise RuntimeError("prewarm did not cover the closed epoch; sync public evidence before paying")
                    generations = {self.evidence_token(result) for result in results}
                    if len(generations) != 1:
                        raise RuntimeError("evidence invalidated during prewarm; refusing payments")
                    self.prewarm_ready = (self.prewarm_key, generations.pop())
                    self.prewarm_task = None
                    emit("epoch_prewarm", state="ready", epoch=job_epoch - 1,
                         profile_checks=sum(result["profiles"] for result in results))
        if key is None:
            return True
        if self.prewarm_ready is not None and self.prewarm_ready[0] == key:
            status = await self.cli.call("treasury", "evidence-generation")
            if self.evidence_token(status) == self.prewarm_ready[1]:
                return True
            self.prewarm_ready = None
        if self.prewarm_task is None:
            self.prewarm_key = key
            self.prewarm_token = self.evidence_token(await self.cli.call("treasury", "evidence-generation"))
            self.prewarm_task = asyncio.create_task(self.prewarm())
            emit("epoch_prewarm", state="started", epoch=epoch - 1, entry_hash=key[1])
        # An ordinary epoch advance must not cancel a long-running build. Once
        # it finishes, catch up newer epochs while continuing to mine.
        if mode == "controlled":
            # Preserve an interrupted payment's ID and arguments. No payment is
            # created while prewarming; existing broadcasts can still be mined.
            for record in self.journal.active_rounds():
                if record["mode"] == "prewarm" or record["mining_requested"]:
                    await self.mine_round(record)
                else:
                    self.journal.defer(record["id"])
            tip = await self.rpc.call("getbestblockhash")
            current = await self.rpc.call("getblock", [tip, 1])
            mempool = await self.rpc.call("getrawmempool")
            if mempool and not self.config["allow_external_transactions"]:
                known = set()
                for operation in self.journal.unfinished():
                    saved = await self.lookup(operation)
                    if saved:
                        known.add(saved["txid"])
                if set(mempool) - known:
                    raise RuntimeError("unexpected external mempool traffic during prewarm")
            round_id = self.journal.new_round(tip, current["height"], "prewarm", 0, expected=mempool)
            if round_id is not None:
                record = next(row for row in self.journal.active_rounds() if row["id"] == round_id)
                await self.mine_round(record)
            # Yield even with immediate fake RPCs, and let background sync/proving run.
            await asyncio.sleep(0)
        return False

    async def round(self, mode, bootstrap=False):
        info, _ = await self.network()
        if not await self.evidence_ready(info, mode):
            return True  # Background proof work never blocks empty block production.
        balances = await self.snapshot()
        # Sync can discover an in-epoch reorg that leaves the entry hash intact.
        # Check the cache generation again before recovery or planning payments.
        info, _ = await self.network()
        if not await self.evidence_ready(info, mode):
            return True
        if not await self.sync_faucet():
            return False  # Bounded historical scan; do not spend against incomplete accounting.
        info, _ = await self.network()
        self.journal.resume_deferred(info["bestblockhash"], info["blocks"], mode)
        await self.recover()
        active = self.journal.active_rounds()
        if active:
            for record in active:
                if record["mode"] in ("controlled", "prewarm"):
                    if mode != "controlled":
                        raise RuntimeError("resume unfinished controlled round in controlled mode")
                    await self.mine_round(record)
                else:
                    self.journal.finish(record["id"])
            return True
        info, _ = await self.network()
        tip, height = info["bestblockhash"], info["blocks"]
        if (mode == "controlled" and not self.config["allow_external_transactions"] and
                await self.rpc.call("getrawmempool")):
            raise RuntimeError("controlled mode requires an empty mempool before a new round")
        target = 1 if bootstrap else self.rng.randint(0, self.config["max_transactions"])
        round_id = self.journal.new_round(tip, height, mode, target)
        if round_id is None:
            return False
        emit("round", round_id=round_id, mode=mode, height=height, target=target, bootstrap=bootstrap)
        if target:
            busy = {operation["wallet"] for operation in self.journal.unfinished()}
            utxos = await self.funding_utxos(busy, balances)
            feed = await self.rpc.call("gettachyonblock", [str(height)])
            tachyon_allowed = (feed["poolHeight"] + 1) % feed["epochLength"] != 0
            payments = []
            faucet_payment = self.faucet.payment(balances, utxos, busy, tachyon_allowed)
            if faucet_payment:
                payments.append(faucet_payment[0])
                busy.add("treasury")
            funding = funding_payment(self.config, balances, utxos, self.addresses, busy,
                                      self.faucet.protected_zat())
            if funding and tachyon_allowed:
                payments.append(funding)
                busy.add("treasury")
            if not bootstrap:
                shuffled = [wallet for wallet in self.wallets if wallet not in busy]
                self.rng.shuffle(shuffled)
                for wallet in shuffled:
                    if len(payments) >= target:
                        break
                    payment = choose_payment(self.config, self.rng, wallet, balances, utxos,
                                             self.addresses, tachyon_allowed)
                    if payment:
                        payments.append(payment)
            # Entire batch is durable before any child process can broadcast.
            operations = [self.journal.plan(round_id, *payment,
                            faucet=faucet_payment[1] if faucet_payment and index == 0 else None)
                          for index, payment in enumerate(payments)]
            await asyncio.gather(*(self.execute(operation) for operation in operations))
            if len(operations) < target:
                emit("underfilled", target=target, planned=len(operations),
                     reason="insufficient independent confirmed inputs or epoch boundary")
        if mode == "controlled":
            record = next(row for row in self.journal.active_rounds() if row["id"] == round_id)
            await self.mine_round(record)
        else:
            self.journal.finish(round_id)
        return True

    async def run(self, mode, rounds, bootstrap=False):
        try:
            await self.run_rounds(mode, rounds, bootstrap)
        finally:
            if self.prewarm_task:
                self.prewarm_task.cancel()
                await asyncio.gather(self.prewarm_task, return_exceptions=True)

    async def run_rounds(self, mode, rounds, bootstrap=False):
        done = 0
        last_tip = None
        while not rounds or done < rounds:
            tip = await self.rpc.call("getbestblockhash")
            catching_up = self.faucet.tracked and not self.faucet.caught_up
            if (mode == "controlled" or tip != last_tip or catching_up) and await self.round(mode, bootstrap):
                done += 1
            last_tip = tip
            if not rounds or done < rounds:
                await asyncio.sleep(self.config["poll_seconds"])

    async def status(self):
        balances = await self.snapshot()
        await self.sync_faucet()
        for wallet, balance in balances.items():
            emit("balance", wallet=wallet, available_zat=balance["available_zat"],
                 transparent=balance["transparent"], notes=len(balance["notes"]))
        emit("journal", unfinished=len(self.journal.unfinished()), active_rounds=self.journal.active_rounds())
