"""Faucet tests use synthetic chains, temporary journals and mock devtool calls only."""

import copy
import hashlib
import json
import random
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from .faucet import BASE58, Faucet, transparent_script, validate_config
from .planner import FEE, funding_payment
from .runtime import CliError, Journal, load_config
from .runner import Runner
from .test_workload import fixture


def address(byte, prefix=b"\x1d\x25"):
    payload = prefix + bytes([byte]) * 20
    raw = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, digit = divmod(value, 58)
        encoded = BASE58[digit] + encoded
    return encoded


TREASURY, FAUCET, OTHER = address(1), address(2), address(3)


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


class Chain:
    def __init__(self):
        self.blocks = {}
        self.txs = {}
        self.calls = []
        self.grow(8)

    @property
    def height(self):
        return max(self.blocks)

    @property
    def tip(self):
        return self.blocks[self.height]["hash"]

    def grow(self, height, reward=625_000_003, branch="original"):
        for h in range(max(self.blocks, default=-1) + 1, height + 1):
            block_hash, txid = digest((branch, h)), digest((branch, h, "coinbase"))
            self.blocks[h] = dict(hash=block_hash, height=h, tx=[txid],
                previousblockhash=self.blocks[h - 1]["hash"] if h else "00" * 32)
            self.txs[block_hash] = dict(txid=txid, blockhash=block_hash, vin=[dict(coinbase="00")], vout=[
                dict(n=0, valueZat=reward, scriptPubKey=dict(hex=transparent_script(TREASURY))),
                dict(n=1, valueZat=10_000_000, scriptPubKey=dict(hex=transparent_script(OTHER)))])

    def reorg(self, first, reward=0):
        height = self.height
        self.blocks = {h: block for h, block in self.blocks.items() if h < first}
        self.grow(height, reward, "reorg")

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "getblockhash":
            return self.blocks[params[0]]["hash"]
        if method == "getblock":
            return copy.deepcopy(self.blocks[int(params[0])])
        if method == "getrawtransaction":
            tx = self.txs[params[2]]
            assert params[0] == tx["txid"] and params[1] == 1
            return copy.deepcopy(tx)
        raise AssertionError((method, params))


class ConfigTests(unittest.TestCase):
    def test_addresses_and_integer_policy(self):
        self.assertEqual(transparent_script(FAUCET), "76a914" + "02" * 20 + "88ac")
        self.assertEqual(transparent_script(address(2, b"\x1c\xba")), "a914" + "02" * 20 + "87")
        for invalid in (address(2, b"\x1c\xb8"), "tachyon-test:abc", FAUCET[:-1] + "0", "", None):
            with self.assertRaises(ValueError):
                validate_config(dict(address=invalid))
        for key, value in (("reward_share_bps", True), ("reward_share_bps", 10000),
                           ("reward_share_bps", 0), ("reward_share_bps", 1.5),
                           ("payout_interval_blocks", 0), ("min_payout_zat", 1), ("unknown", 7)):
            with self.assertRaises(ValueError):
                validate_config(dict(address=FAUCET) | {key: value})

    def test_optional_config_loads_with_defaults(self):
        source = Path(__file__).parent.parent / "config.example.toml"
        self.assertNotIn("faucet", load_config(source))
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            config.write_text(source.read_text() + f'\n[faucet]\naddress = "{FAUCET}"\n')
            self.assertEqual(load_config(config)["faucet"], dict(address=FAUCET,
                reward_share_bps=1000, payout_interval_blocks=10, min_payout_zat=100_000))


class FaucetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.journal = Journal(self.path)
        self.config = validate_config(dict(address=FAUCET))
        self.faucet = Faucet(self.journal, self.config)
        self.chain = Chain()
        self.addresses = {"treasury": dict(funding_address=TREASURY)}
        self.faucet.initialize(self.chain.height, self.chain.tip, self.addresses)

    async def asyncTearDown(self):
        self.journal.close()
        self.temp.cleanup()

    async def sync(self, height):
        self.chain.grow(height)
        return await self.faucet.sync(self.chain, height, self.chain.tip)

    def operation(self, amount, status="planned"):
        round_id = self.journal.new_round(digest(("round", self.journal.db.total_changes)),
                                           self.chain.height, "live", 1)
        policy = self.faucet.balances()[0]["policy_id"]
        op = self.journal.plan(round_id, "treasury", None,
            ["unshield", "--to", FAUCET, "--amount-zat", str(amount)], faucet=(policy, amount, "payout"))
        self.journal.update(op["id"], status=status, txid="payment" if status != "planned" else None)
        return self.journal.get(op["id"])

    async def test_maturity_batches_exact_integer_rewards_and_no_historical_balance(self):
        await self.sync(107)  # First allocated block 9 has only 99 confirmations.
        row = self.faucet.balances()[0]
        self.assertEqual(row["allocated_zat"], 99 * 62_500_000)
        self.assertEqual(row["owed_zat"], 0)
        await self.sync(108)
        row = self.faucet.balances()[0]
        self.assertEqual(row["owed_zat"], 62_500_000)
        self.assertEqual(row["due_zat"], 0)
        await self.sync(117)
        row = self.faucet.balances()[0]
        self.assertEqual(row["due_zat"], 625_000_000)
        self.assertEqual(self.journal.db.execute("SELECT SUM(reward_zat) FROM faucet_blocks").fetchone()[0],
                         109 * 625_000_003)
        self.assertFalse(any(method == "getaddressutxos" for method, _ in self.chain.calls))

    async def test_partial_payout_fee_and_pending_reconciliation(self):
        await self.sync(117)
        balances = {"treasury": {"notes": [{"status": "available", "value_zat": 200_000_000}]}}
        payment, tag = self.faucet.payment(balances, {}, set(), True)
        self.assertEqual(payment[2], ["unshield", "--to", FAUCET, "--amount-zat", str(200_000_000 - FEE)])
        op = self.operation(tag[1])
        row = self.faucet.balances()[0]
        self.assertEqual(row["in_flight_zat"], tag[1])
        self.assertEqual(row["due_zat"], 625_000_000 - tag[1])
        self.assertIsNone(self.faucet.payment(balances, {}, {"treasury"}, True))
        self.assertIsNone(self.faucet.payment(balances, {}, set(), False))
        self.journal.reconcile("treasury", [dict(operation_id=op["id"], txid="payment", status="mined")])
        self.assertEqual(self.faucet.balances()[0]["paid_zat"], tag[1])
        self.journal.reconcile("treasury", [dict(operation_id=op["id"], txid="payment", status="pending")])
        self.assertEqual(self.faucet.balances()[0]["paid_zat"], 0)
        self.assertEqual(self.faucet.balances()[0]["in_flight_zat"], tag[1])
        self.journal.reconcile("treasury", [dict(operation_id=op["id"], txid="payment", status="expired")])
        self.assertEqual(self.faucet.balances()[0]["due_zat"], 625_000_000)

    async def test_multiple_treasury_outputs_are_summed_before_rounding(self):
        self.chain.grow(9)
        self.chain.txs[self.chain.tip]["vout"].append(dict(n=2, valueZat=7,
            scriptPubKey=dict(hex=transparent_script(TREASURY))))
        await self.sync(9)
        row = self.journal.db.execute("SELECT * FROM faucet_blocks WHERE height=9").fetchone()
        self.assertEqual(row["reward_zat"], 625_000_010)
        self.assertEqual(row["allocation_zat"], 62_500_001)

    async def test_last_partial_policy_batch_is_released_when_mature(self):
        await self.sync(13)  # Five rewards, less than the configured batch of ten.
        self.faucet.config = validate_config(dict(address=OTHER))
        await self.sync(13)
        await self.sync(112)
        self.assertEqual(self.faucet.balances()[0]["due_zat"], 5 * 62_500_000)

    async def test_replenishment_and_protection_before_batch_is_due(self):
        await self.sync(108)
        config, balances, utxos, addresses = fixture()
        balances["treasury"]["notes"] = [dict(status="available", value_zat=70_000_000)]
        protected = self.faucet.protected_zat()
        self.assertEqual(protected, 62_500_000 + FEE)
        self.assertIsNone(funding_payment(config, balances, {}, addresses, set(), protected))
        await self.sync(117)
        balances["treasury"]["notes"] = []
        payment, tag = self.faucet.payment(balances, utxos, set(), True)
        self.assertEqual(payment[2], ["shield", "--txid", "treasury", "--index", "0"])
        self.assertEqual(tag[1:], (0, "replenish"))
        self.assertIsNone(self.faucet.payment(balances, {}, set(), True))

    async def test_bounded_scan_and_restart_never_double_count_or_pay_early(self):
        self.chain.grow(117)
        with patch("tachyon_workload.faucet.SCAN_BATCH", 3):
            self.assertFalse(await self.faucet.sync(self.chain, 117, self.chain.tip))
        self.assertEqual(self.faucet.status()["scanned_height"], 11)
        self.assertIsNone(self.faucet.payment({}, {}, set(), True))
        self.journal.close()
        self.journal = Journal(self.path)
        self.faucet = Faucet(self.journal, self.config)
        self.faucet.initialize(117, self.chain.tip, self.addresses)
        await self.sync(117)
        first = self.faucet.balances()
        self.chain.calls.clear()
        await self.sync(117)
        self.assertEqual(self.faucet.balances(), first)
        self.assertTrue(all(method == "getblockhash" for method, _ in self.chain.calls))

    async def test_reorg_replaces_rewards_and_carries_overpayment_forward(self):
        await self.sync(117)
        self.operation(625_000_000, "mined")
        self.chain.reorg(9, reward=0)
        await self.sync(117)
        row = self.faucet.balances()[0]
        self.assertEqual(row["allocated_zat"], 0)
        self.assertEqual(row["credit_zat"], 625_000_000)
        self.assertEqual(row["due_zat"], 0)
        await self.sync(226)
        row = self.faucet.balances()[0]
        self.assertEqual(row["credit_zat"], 0)
        self.assertEqual(row["owed_zat"], 0)  # Ten new mature rewards repay the credit.
        await self.sync(236)
        self.assertEqual(self.faucet.balances()[0]["owed_zat"], 625_000_000)

    async def test_reorg_of_baseline_and_shorter_chain(self):
        await self.sync(117)
        self.chain.reorg(7, reward=0)
        await self.sync(117)
        self.assertEqual(self.faucet.balances()[0]["allocated_zat"], 0)
        self.chain.blocks = {h: b for h, b in self.chain.blocks.items() if h <= 10}
        await self.sync(10)
        self.assertEqual(self.faucet.status()["scanned_height"], 10)

    async def test_policy_edits_apply_only_to_future_blocks_and_keep_pending_destination(self):
        await self.sync(117)
        op = self.operation(100_000)
        self.faucet.config = validate_config(dict(address=OTHER, reward_share_bps=2000))
        await self.sync(117)
        await self.sync(227)
        old, new = self.faucet.balances()
        self.assertEqual(old["address"], FAUCET)
        self.assertEqual(old["allocated_zat"], 109 * 62_500_000)
        self.assertEqual(new["address"], OTHER)
        self.assertEqual(new["allocated_zat"], 110 * 125_000_000)
        self.assertEqual(json.loads(self.journal.get(op["id"])["args"])[2], FAUCET)

    async def test_disable_preserves_debt_and_pending_operations_without_new_accrual(self):
        await self.sync(117)
        op = self.operation(100_000)
        before = self.faucet.balances()[0]["allocated_zat"]
        self.faucet.config = None
        await self.sync(117)
        await self.sync(130)
        self.assertEqual(self.faucet.balances()[0]["allocated_zat"], before)
        self.assertIsNone(self.faucet.payment({}, {}, set(), True))
        self.assertEqual(self.journal.unfinished()[0]["id"], op["id"])

    async def test_malformed_coinbase_rolls_back_scan(self):
        self.chain.grow(10)
        self.chain.txs[self.chain.tip]["vout"][0]["valueZat"] = 1.5
        with self.assertRaisesRegex(RuntimeError, "integer valueZat"):
            await self.sync(10)
        self.assertEqual(self.faucet.status()["scanned_height"], 8)

    async def test_tip_change_at_commit_does_not_accrue_mixed_chains(self):
        self.chain.grow(10)
        original = self.chain.call
        calls = 0

        async def reorg_after_scan(method, params):
            nonlocal calls
            if method == "getblockhash" and params == [10]:
                calls += 1
                if calls == 2:
                    return "ff" * 32
            return await original(method, params)

        self.chain.call = reorg_after_scan
        with self.assertRaisesRegex(RuntimeError, "chain changed"):
            await self.sync(10)
        self.assertEqual(self.faucet.status()["scanned_height"], 8)

    async def test_external_address_enforced_and_atomic_operation_tag(self):
        with self.assertRaisesRegex(ValueError, "external"):
            Faucet(self.journal, dict(address=TREASURY)).initialize(8, self.chain.tip, self.addresses)
        await self.sync(117)
        round_id = self.journal.new_round("round", 117, "live", 1)
        with self.assertRaisesRegex(ValueError, "accounting"):
            self.journal.plan(round_id, "treasury", None, ["unshield", "--amount-zat", "10"],
                              faucet=(1, 20, "payout"))
        self.assertEqual(self.journal.round_operations(round_id), [])

    async def test_credit_crosses_rate_changes_but_not_recipients(self):
        await self.sync(117)
        self.operation(625_000_000, "mined")
        self.faucet.config = validate_config(dict(address=FAUCET, reward_share_bps=2000))
        await self.sync(117)
        self.chain.reorg(9, reward=0)
        await self.sync(117)
        await self.sync(226)
        old, new = self.faucet.balances()
        self.assertEqual(old["credit_zat"], 0)
        self.assertEqual(new["owed_zat"], 625_000_000)  # 1.25B newly mature minus old credit.
        self.assertEqual(new["due_zat"], 625_000_000)
        self.assertEqual(new["credit_zat"], 0)

    async def test_changed_recipient_does_not_inherit_another_recipients_credit(self):
        await self.sync(117)
        self.operation(625_000_000, "mined")
        self.faucet.config = validate_config(dict(address=OTHER))
        await self.sync(117)
        self.chain.reorg(9, reward=0)
        await self.sync(117)
        await self.sync(226)
        old, new = self.faucet.balances()
        self.assertEqual(old["credit_zat"], 625_000_000)
        self.assertEqual(new["due_zat"], 625_000_000)


class FaucetRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config, self.balances, self.utxos, addresses = fixture()
        config.update(state_dir=Path(self.temp.name) / "state", wallet_root=Path(self.temp.name) / "wallets",
                      faucet=dict(address=FAUCET), max_transactions=2)
        self.runner = Runner(config)
        self.runner.wallets = list(addresses)
        addresses["treasury"]["funding_address"] = TREASURY
        addresses["alice"]["funding_address"] = address(4)
        addresses["bob"]["funding_address"] = address(5)
        self.runner.addresses = addresses
        self.chain = Chain()
        self.runner.faucet.initialize(8, self.chain.tip, addresses)
        self.chain.grow(117)
        self.runner.wallet_tips["treasury"] = (117, self.chain.tip)
        self.runner.network = AsyncMock(return_value=(dict(blocks=117, bestblockhash=self.chain.tip), "identity"))
        self.runner.snapshot = AsyncMock(return_value=self.balances)
        self.runner.funding_utxos = AsyncMock(return_value=self.utxos)
        self.balances["treasury"]["notes"] = [dict(status="available", value_zat=1_000_000_000)]
        self.mempool = []
        self.runner.rpc = AsyncMock()
        self.runner.rpc.call.side_effect = self.rpc

    async def asyncTearDown(self):
        self.runner.journal.close()
        self.temp.cleanup()

    async def rpc(self, method, params=None):
        if method == "gettachyonblock":
            return dict(poolHeight=109, epochLength=4096)
        if method == "getrawmempool":
            return self.mempool.copy()
        if method == "getbestblockhash":
            return self.chain.tip
        if method == "generate":
            assert params == [1]
            self.chain.grow(self.chain.height + 1)
            self.chain.blocks[self.chain.height]["tx"].extend(self.mempool)
            return [self.chain.tip]
        if method == "getblock" and not params[0].isdigit():
            return next(block for block in self.chain.blocks.values() if block["hash"] == params[0])
        return await self.chain.call(method, params)

    async def test_zero_target_accrues_without_payout_or_replenishment(self):
        runner = self.runner
        runner.rng = Mock()
        runner.rng.randint.return_value = 0
        runner.execute = AsyncMock()
        await runner.round("controlled")
        runner.execute.assert_not_awaited()
        runner.funding_utxos.assert_not_awaited()
        self.assertEqual(runner.faucet.balances()[0]["due_zat"], 625_000_000)
        self.assertEqual(len(self.chain.blocks[self.chain.height]["tx"]), 1)

    async def test_budget_priority_and_one_operation_per_wallet(self):
        runner = self.runner
        runner.rng = random.Random(5)
        runner.execute = AsyncMock()
        await runner.round("live")
        ops = runner.journal.round_operations(1)
        self.assertEqual(len(ops), 2)
        self.assertEqual(len({op["wallet"] for op in ops}), 2)
        self.assertEqual(ops[0]["kind"], "unshield")
        self.assertIsNone(ops[0]["recipient"])
        self.assertEqual(json.loads(ops[0]["args"])[2], FAUCET)
        self.assertEqual(runner.faucet.balances()[0]["in_flight_zat"], 625_000_000)

    async def test_controlled_block_contains_the_journaled_faucet_payment(self):
        runner = self.runner
        runner.rng = Mock()
        runner.rng.randint.return_value = 1

        async def execute(op):
            txid = digest(op["id"])
            runner.journal.update(op["id"], status="pending", txid=txid)
            self.mempool.append(txid)
            return True

        runner.execute = execute
        await runner.round("controlled")
        ops = runner.journal.round_operations(1)
        self.assertEqual(self.chain.blocks[118]["tx"][1:], [ops[0]["txid"]])
        self.assertEqual(ops[0]["kind"], "unshield")
        self.assertEqual(runner.journal.active_rounds(), [])

    async def test_replenishment_is_a_budgeted_treasury_operation(self):
        runner = self.runner
        self.balances["treasury"]["notes"] = []
        runner.rng = Mock()
        runner.rng.randint.return_value = 1
        runner.execute = AsyncMock()
        await runner.round("live")
        ops = runner.journal.round_operations(1)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["kind"], "shield")
        self.assertEqual(runner.faucet.balances()[0]["in_flight_zat"], 0)

    async def test_incomplete_scan_defers_new_rounds_and_can_resume_at_same_tip(self):
        runner = self.runner
        runner.rng = Mock()
        runner.rng.randint.return_value = 0
        runner.execute = AsyncMock()
        with patch("tachyon_workload.faucet.SCAN_BATCH", 50), patch("asyncio.sleep", new_callable=AsyncMock):
            await runner.run("live", rounds=1)
        self.assertTrue(runner.faucet.caught_up)
        self.assertEqual(runner.journal.db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 1)
        runner.execute.assert_not_awaited()

    async def test_ambiguous_payout_recovery_survives_restart_and_disabling_faucet(self):
        runner = self.runner
        await runner.sync_faucet()
        payment, tag = runner.faucet.payment(self.balances, self.utxos, set(), True)
        round_id = runner.journal.new_round(self.chain.tip, 117, "live", 1)
        op = runner.journal.plan(round_id, *payment, faucet=tag)
        saved = {}
        creates = 0

        async def cli(wallet, *args):
            nonlocal creates
            if args[0] == "operation":
                if not saved:
                    raise CliError("unknown operation ID")
                return saved
            if args[0] == "unshield":
                creates += 1
                saved.update(txid="payment", status="pending")
                self.mempool.append("payment")
                raise TimeoutError("accepted, reply lost")
            raise AssertionError("external faucet must not receive confidential receipt imports")

        runner.cli = AsyncMock()
        runner.cli.call.side_effect = cli
        await runner.execute(op)
        config = runner.config.copy()
        config.pop("faucet")
        runner.journal.close()
        self.runner = Runner(config)
        self.runner.cli = runner.cli
        self.runner.rpc = runner.rpc
        await self.runner.recover()
        self.assertEqual(creates, 1)
        self.assertEqual(self.runner.journal.get(op["id"])["txid"], "payment")
        self.assertEqual(self.runner.journal.db.execute("SELECT COUNT(*) FROM faucet_operations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
