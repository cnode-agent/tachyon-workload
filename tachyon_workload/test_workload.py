"""Offline regression tests; no real keys, funds, or node required."""

import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from .planner import FEE, choose_payment, funding_payment
from .runner import Runner
from .runtime import CliError, Devtool, Journal, load_config, private_dir


def fixture():
    config = load_config(Path(__file__).parent.parent / "config.example.toml")
    balances = {name: {"notes": [{"status": "available", "value_zat": 10_000_000}],
                       "available_zat": 10_000_000} for name in ("treasury", "alice", "bob")}
    addresses = {name: {"tachyon_address": "tachyon-test:" + name, "funding_address": "t:" + name}
                 for name in balances}
    utxos = {name: [{"txid": name, "outputIndex": 0, "satoshis": 10_000_000, "coinbase": False}]
             for name in balances}
    return config, balances, utxos, addresses


class PlannerTests(unittest.TestCase):
    def test_external_transactions_option_is_opt_in_and_boolean(self):
        example = Path(__file__).parent.parent / "config.example.toml"
        self.assertFalse(load_config(example)["allow_external_transactions"])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text(example.read_text().replace(
                "allow_external_transactions = false", 'allow_external_transactions = "true"'))
            with self.assertRaisesRegex(ValueError, "must be true or false"):
                load_config(path)

    def test_example_config_keeps_runtime_data_outside_checkout(self):
        config, _, _, _ = fixture()
        checkout = Path(__file__).resolve().parent.parent
        for name in ("devtool", "wallet_root", "state_dir"):
            self.assertFalse(config[name].resolve().is_relative_to(checkout))

    def test_target_range_includes_zero_and_cap(self):
        rng = random.Random(1)
        self.assertEqual({rng.randint(0, 8) for _ in range(1000)}, set(range(9)))

    def test_weighted_mix_and_amounts(self):
        config, balances, utxos, addresses = fixture()
        rng = random.Random(42)
        counts = dict.fromkeys(config["weights"], 0)
        for _ in range(10000):
            wallet, recipient, args = choose_payment(config, rng, "alice", balances, utxos, addresses, True)
            self.assertEqual(wallet, "alice")
            counts[args[0]] += 1
            amount = int(args[1] if args[0] == "shield" else args[-1])
            self.assertLessEqual(amount + FEE, 10_000_000)
            self.assertEqual(recipient, "bob" if args[0] == "send" else None)
        for kind, weight in config["weights"].items():
            self.assertAlmostEqual(counts[kind] / 10000, weight / 100, delta=0.02)

    def test_coinbases_never_selected_for_random_transparent_payments(self):
        config, balances, utxos, addresses = fixture()
        balances["alice"]["notes"] = []
        utxos["alice"][0]["coinbase"] = True
        self.assertIsNone(choose_payment(config, random.Random(), "alice", balances, utxos, addresses, True))

    def test_epoch_end_only_allows_transparent(self):
        config, balances, utxos, addresses = fixture()
        for _ in range(100):
            payment = choose_payment(config, random.Random(), "alice", balances, utxos, addresses, False)
            self.assertEqual(payment[2][0], "transparent-send")

    def test_reservations_and_single_note_capacity(self):
        config, balances, utxos, addresses = fixture()
        balances["alice"]["notes"] = [{"status": "reserved", "value_zat": 100_000_000}] + [
            {"status": "available", "value_zat": FEE} for _ in range(20)]
        self.assertIsNone(choose_payment(config, random.Random(), "alice", balances, {}, addresses, True))

    def test_treasury_full_shield_then_different_denominations(self):
        config, balances, utxos, addresses = fixture()
        balances["treasury"]["notes"] = []
        utxos["treasury"][0]["satoshis"] = 625_000_000
        payment = funding_payment(config, balances, utxos, addresses, set())
        self.assertEqual(payment, ("treasury", None, ["shield", "--txid", "treasury", "--index", "0"]))
        balances["treasury"]["notes"] = [{"status": "available", "value_zat": 600_000_000}]
        payment = funding_payment(config, balances, utxos, addresses, set())
        self.assertEqual(payment[2][-1], str(config["seed_amount_zat"] * 2))
        self.assertIsNone(funding_payment(config, balances, utxos, addresses, {"treasury"}))

    def test_expired_unconfirmed_receipts_do_not_prevent_refunding(self):
        config, balances, utxos, addresses = fixture()
        balances["alice"]["notes"] = [{"status": "unconfirmed", "value_zat": 100_000_000}] * 3
        balances["treasury"]["notes"][0]["value_zat"] = 100_000_000
        payment = funding_payment(config, balances, utxos, addresses, set())
        self.assertEqual(payment[1], "alice")
        self.assertEqual(payment[2][-1], str(config["seed_amount_zat"]))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.journal = Journal(self.path)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def test_restart_pins_request_and_round(self):
        self.journal.pin("network_id", "chain-a")
        round_id = self.journal.new_round("tip", 12, "live", 1)
        op = self.journal.plan(round_id, "alice", "bob", ["send", "--amount-zat", "123"])
        self.journal.close()
        self.journal = Journal(self.path)
        self.assertEqual(self.journal.get(op["id"]), op)
        self.assertIsNone(self.journal.new_round("tip", 12, "live", 8))
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.journal.pin("network_id", "chain-b")

    def test_reorg_reopens_mined_operation(self):
        round_id = self.journal.new_round("tip", 1, "live", 1)
        op = self.journal.plan(round_id, "alice", None, ["shield", "123"])
        self.journal.update(op["id"], status="mined", txid="tx")
        self.journal.reconcile("alice", [{"operation_id": op["id"], "txid": "tx", "status": "pending"}])
        self.assertEqual(self.journal.unfinished()[0]["id"], op["id"])

    def test_duplicate_sidecar_and_public_directory_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "another sidecar"):
            Journal(self.path)
        public = self.path / "public"
        public.mkdir()
        public.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            private_dir(public)
        self.assertEqual((self.path / "workload.sqlite").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.path / "sidecar.lock").stat().st_mode & 0o777, 0o600)


class FakeCli:
    def __init__(self):
        self.saved = None
        self.creations = 0
        self.imports = 0
        self.fail_import = True
        self.calls = []

    async def call(self, wallet, *args):
        self.calls.append((wallet, args))
        if args[0] == "operation":
            if not self.saved:
                raise CliError("unknown operation ID")
            return self.saved
        if args[0] == "send":
            self.creations += 1
            self.saved = dict(txid="tx", status="pending")
            raise TimeoutError("broadcast timed out after durable commit")
        if args[0] == "export-receipt":
            Path(args[-1]).write_text('{"dummy":"test receipt"}')
        elif args[0] == "receive":
            self.imports += 1
            if self.fail_import:
                self.fail_import = False
                raise CliError("interrupted receipt import")
        return {}


class DevtoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_wallets_share_public_evidence_directory(self):
        config, _, _, _ = fixture()
        class FakeProcess:
            def __init__(self):
                self.returncode = 0
                self.communicate = AsyncMock(return_value=(b"{}", b""))

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=FakeProcess())) as spawn:
            await Devtool(config).call("alice", "sync")
        call = spawn.await_args
        assert call is not None
        env = call.kwargs["env"]
        self.assertEqual(
            env["TACHYON_EVIDENCE_DIR"],
            str(Path(str(config["wallet_root"])) / ".evidence"),
        )


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config, _, _, _ = fixture()
        config.update(state_dir=Path(self.temp.name) / "state", wallet_root=Path(self.temp.name) / "wallets")
        self.runner = Runner(config, seed=1)

    async def asyncTearDown(self):
        self.runner.journal.close()
        self.temp.cleanup()

    async def test_ambiguous_broadcast_and_receipt_crash_create_one_payment(self):
        runner = self.runner
        runner.cli = FakeCli()
        runner.rpc = AsyncMock()
        runner.rpc.call.return_value = ["tx"]
        round_id = runner.journal.new_round("tip", 12, "live", 1)
        operation = runner.journal.plan(round_id, "alice", "bob", ["send", "--amount-zat", "123"])
        runner.wallet_tips["alice"] = (12, "snapshot-hash")
        with self.assertRaisesRegex(CliError, "interrupted receipt"):
            await runner.execute(operation)
        await runner.execute(runner.journal.get(operation["id"]))
        payment_call = next(args for wallet, args in runner.cli.calls if args[0] == "send")
        self.assertEqual(payment_call[-2:], ("--expected-tip", "snapshot-hash"))
        self.assertEqual(runner.cli.creations, 1)
        self.assertEqual(runner.cli.imports, 2)
        self.assertEqual(runner.journal.get(operation["id"])["receipt_delivered"], 1)
        self.assertEqual(len(list(runner.receipts.glob("*.json"))), 1)

    async def test_snapshot_skips_full_history_without_active_operations(self):
        class StatusCli:
            async def sync(self, wallet):
                return {"height": 12, "hash": "tip", "reorg": False}

            async def call(self, wallet, *args):
                if args[0] == "balance-summary":
                    return {"available_zat": 1, "transparent": None, "notes": []}
                if args[0] == "transaction-status":
                    return {"transactions": []}
                raise AssertionError("full transaction history was requested")

        runner = self.runner
        runner.wallets = ["alice"]
        runner.cli = StatusCli()
        await runner.snapshot()

    async def test_funding_discovery_uses_wallet_candidates_without_rpc(self):
        runner = self.runner
        runner.wallets = ["alice"]
        runner.rpc = AsyncMock()
        candidates = [{"txid": "tx", "outputIndex": 0, "satoshis": 100_000, "coinbase": False}]
        balances = {"alice": {"transparent": {"funding_candidates": candidates}}}
        result = await runner.funding_utxos(set(), balances)
        self.assertEqual(result, {"alice": candidates})
        runner.rpc.call.assert_not_called()

    async def test_zero_round_has_no_hidden_funding(self):
        runner = self.runner
        runner.config["max_transactions"] = 0
        runner.network = AsyncMock(return_value=({"blocks": 12, "bestblockhash": "tip"}, "identity"))
        runner.snapshot = AsyncMock(return_value={})
        runner.recover = AsyncMock()
        runner.funding_utxos = AsyncMock()
        runner.execute = AsyncMock()
        await runner.round("live")
        runner.execute.assert_not_awaited()
        runner.funding_utxos.assert_not_awaited()
        self.assertFalse(await runner.round("live"))

    async def test_generate_timeout_recovers_without_mining_another_block(self):
        runner = self.runner
        round_id = runner.journal.new_round("tip", 12, "controlled", 0)
        record = runner.journal.active_rounds()[0]
        mined = False
        calls = 0

        async def rpc(method, params=None):
            nonlocal mined, calls
            if method == "getbestblockhash":
                return "successor" if mined else "tip"
            if method == "getrawmempool":
                return []
            if method == "generate":
                mined = True
                calls += 1
                raise TimeoutError("mined but response lost")
            if method == "getblock":
                return dict(previousblockhash="tip", tx=["coinbase"], height=13, hash="successor")
            raise AssertionError(method)

        runner.rpc = AsyncMock()
        runner.rpc.call.side_effect = rpc
        with self.assertRaises(TimeoutError):
            await runner.mine_round(record)
        await runner.mine_round(record)
        self.assertEqual(calls, 1)
        self.assertEqual(runner.journal.active_rounds(), [])
        self.assertEqual(runner.journal.round_operations(round_id), [])

    async def test_controlled_mode_rejects_unrelated_mempool_transactions(self):
        runner = self.runner
        runner.journal.new_round("tip", 12, "controlled", 0)
        runner.rpc = AsyncMock()
        runner.rpc.call.side_effect = ["tip", ["someone-elses-tx"]]
        with self.assertRaisesRegex(RuntimeError, "mempool differs"):
            await runner.mine_round(runner.journal.active_rounds()[0])

    async def test_ambiguous_generate_without_visible_successor_never_retries(self):
        runner = self.runner
        runner.journal.new_round("tip", 12, "controlled", 0)
        record = runner.journal.active_rounds()[0]
        runner.rpc = AsyncMock()
        runner.rpc.call.side_effect = ["tip", [], TimeoutError("still processing"), "tip", []]
        with self.assertRaises(TimeoutError):
            await runner.mine_round(record)
        with self.assertRaisesRegex(RuntimeError, "outcome remains ambiguous"):
            await runner.mine_round(record)
        self.assertEqual(sum(call.args[0] == "generate" for call in runner.rpc.call.await_args_list), 1)

    async def test_external_payment_can_be_mined_in_zero_workload_round(self):
        runner = self.runner
        runner.config.update(allow_external_transactions=True, max_transactions=0)
        runner.network = AsyncMock(return_value=({"blocks": 12, "bestblockhash": "tip"}, "identity"))
        runner.snapshot = AsyncMock(return_value={})
        runner.recover = AsyncMock()
        runner.rpc = AsyncMock()
        runner.rpc.call.side_effect = ["tip", ["faucet-tx"], ["next"],
            dict(previousblockhash="tip", tx=["coinbase", "faucet-tx"], height=13, hash="next")]
        await runner.round("controlled")
        self.assertFalse(runner.journal.active_rounds())
        self.assertEqual(sum(call.args[0] == "generate" for call in runner.rpc.call.await_args_list), 1)

    async def test_external_traffic_does_not_allow_missing_workload_payment(self):
        runner = self.runner
        runner.config["allow_external_transactions"] = True
        round_id = runner.journal.new_round("tip", 12, "controlled", 1)
        operation = runner.journal.plan(round_id, "alice", None, ["shield", "123"])
        runner.journal.update(operation["id"], status="pending", txid="ours")
        record = runner.journal.active_rounds()[0]
        runner.rpc = AsyncMock()
        runner.rpc.call.side_effect = ["tip", ["faucet-tx"]]
        with self.assertRaisesRegex(RuntimeError, "mempool differs"):
            await runner.mine_round(record)
        runner.rpc.call.side_effect = ["tip", ["ours", "faucet-tx"], ["next"],
            dict(previousblockhash="tip", tx=["coinbase", "faucet-tx"], height=13, hash="next")]
        with self.assertRaisesRegex(RuntimeError, "did not match"):
            await runner.mine_round(record)
        self.assertTrue(runner.journal.active_rounds())

    async def test_generate_timeout_with_external_traffic_recovers_same_block(self):
        runner = self.runner
        runner.config["allow_external_transactions"] = True
        runner.journal.new_round("tip", 12, "controlled", 0)
        record = runner.journal.active_rounds()[0]
        runner.rpc = AsyncMock()
        runner.rpc.call.side_effect = ["tip", ["faucet-tx"], TimeoutError("response lost"), "next",
            dict(previousblockhash="tip", tx=["coinbase", "faucet-tx"], height=13, hash="next")]
        with self.assertRaises(TimeoutError):
            await runner.mine_round(record)
        await runner.mine_round(record)
        self.assertFalse(runner.journal.active_rounds())
        self.assertEqual(sum(call.args[0] == "generate" for call in runner.rpc.call.await_args_list), 1)

    async def test_mainnet_rejected_before_wallet_commands(self):
        runner = self.runner
        runner.rpc = AsyncMock()
        runner.rpc.call.return_value = dict(chain="main", pruned=False)
        runner.cli = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "private test/regtest"):
            await runner.initialize()
        runner.cli.call.assert_not_awaited()

    async def test_budget_includes_refill_and_never_spends_same_wallet_twice(self):
        runner = self.runner
        config, balances, utxos, addresses = fixture()
        runner.config = config | {"max_transactions": 2}
        balances["treasury"]["notes"][0]["value_zat"] = 100_000_000
        runner.wallets = list(balances)
        runner.addresses = addresses
        runner.rng = random.Random(5)  # first draw from 0..2 is 2
        runner.network = AsyncMock(return_value=({"blocks": 12, "bestblockhash": "tip"}, "identity"))
        runner.snapshot = AsyncMock(return_value=balances)
        runner.recover = AsyncMock()
        runner.funding_utxos = AsyncMock(return_value=utxos)
        runner.rpc = AsyncMock()
        runner.rpc.call.return_value = {"poolHeight": 4, "epochLength": 4096}
        runner.execute = AsyncMock()
        await runner.round("live")
        operations = runner.journal.round_operations(1)
        self.assertEqual(len(operations), 2)
        self.assertEqual(len({op["wallet"] for op in operations}), 2)
        self.assertIn("treasury", {op["wallet"] for op in operations})


if __name__ == "__main__":
    os.umask(0o077)
    unittest.main()
