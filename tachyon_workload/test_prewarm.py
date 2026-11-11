"""Virtual-time boundary tests: no RPC to a live node and no real mining."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from .runner import Runner
from .runtime import CliError, Devtool
from .test_workload import fixture


class BoundaryNode:
    def __init__(self):
        self.height = 4104
        self.entry = "entry-a"
        self.blocks = {}
        self.calls = []
        self.mempool = []

    def tip(self):
        return f"block-{self.height}"

    async def call(self, method, params=None):
        self.calls.append(method)
        if method == "gettachyonblock":
            return {"poolHeight": self.height - 8, "epochLength": 4096}
        if method == "getblockhash":
            return self.entry
        if method == "getbestblockhash":
            return self.tip()
        if method == "getrawmempool":
            return self.mempool.copy()
        if method == "getblock":
            key = f"block-{params[0]}" if str(params[0]).isdigit() else params[0]
            return self.blocks.get(key, {"hash":self.tip(), "height":self.height, "tx":["coinbase"]})
        if method == "generate":
            previous = self.tip()
            self.height += 1
            self.blocks[self.tip()] = {"hash":self.tip(), "height":self.height,
                                        "previousblockhash":previous, "tx":["coinbase", *self.mempool]}
            self.mempool.clear()
            return [self.tip()]
        raise AssertionError(method)


class PrewarmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config, _, _, _ = fixture()
        config.update(state_dir=Path(self.temp.name)/"state", wallet_root=Path(self.temp.name)/"wallets",
                      activation_height=8, wallet_count=16, concurrency=4, max_transactions=0)
        self.runner = Runner(config)
        self.node = BoundaryNode()
        self.runner.rpc = self.node
        async def network():
            return {"blocks":self.node.height,"bestblockhash":self.node.tip()}, "network"
        self.runner.network = network
        self.runner.snapshot = AsyncMock(return_value={})
        self.runner.recover = AsyncMock()
        self.runner.cli = AsyncMock()
        self.runner.cli.call.return_value = {"generation":0,"namespace":"test"}
        self.ready = asyncio.Event()
        async def slow():
            await self.ready.wait()
            return [{"profiles":7,"through_epoch":0,"generation":0,"namespace":"test"} for _ in range(17)]
        self.runner.prewarm = slow

    async def asyncTearDown(self):
        if self.runner.prewarm_task:
            self.runner.prewarm_task.cancel()
            await asyncio.gather(self.runner.prewarm_task, return_exceptions=True)
        self.runner.journal.close()
        self.temp.cleanup()

    async def test_eighteen_minutes_prewarm_keeps_mining_then_resumes(self):
        r = self.runner
        # 540 polling ticks represent eighteen minutes at the configured 2s interval.
        for _ in range(540):
            await r.round("controlled")
        self.assertEqual(self.node.height, 4644)
        self.assertEqual(self.node.calls.count("generate"),540)
        r.snapshot.assert_not_awaited()
        r.recover.assert_not_awaited()
        self.assertEqual(r.journal.unfinished(),[])
        self.assertEqual(r.journal.active_rounds(),[])
        self.ready.set()
        await asyncio.sleep(0)
        await r.round("controlled")
        r.snapshot.assert_awaited_once()
        r.recover.assert_awaited_once()
        self.assertEqual(self.node.height,4645)
        self.assertEqual(len(self.node.blocks),541)
        self.assertTrue(all(len(block["tx"]) == 1 for block in self.node.blocks.values()))

    async def test_interrupted_payment_keeps_operation_id_across_prewarm(self):
        r = self.runner
        rid = r.journal.new_round(self.node.tip(),self.node.height,"controlled",1)
        op = r.journal.plan(rid,"sim-001",None,["send","--amount-zat","10"])
        await r.round("controlled")
        await r.round("controlled")
        self.assertEqual(r.journal.get(op["id"])["status"],"planned")
        self.assertEqual(len(r.journal.unfinished()),1)
        self.ready.set()
        await asyncio.sleep(0)
        async def recover():
            self.assertEqual(r.journal.unfinished()[0]["id"],op["id"])
            # A simulated expired operation is not sent twice or included in mining.
            r.journal.update(op["id"], status="expired")
        r.recover = recover
        await r.round("controlled")
        self.assertEqual(r.journal.get(op["id"])["status"],"expired")
        self.assertEqual(r.journal.active_rounds(),[])
        self.assertEqual(r.journal.db.execute("SELECT count(*) FROM operations").fetchone()[0],1)

    async def test_reorg_during_and_after_prewarm_never_enables_payments(self):
        r=self.runner
        await r.round("controlled")
        old=r.prewarm_task
        self.node.entry="entry-b"
        await r.round("controlled")
        self.assertTrue(old.cancelled())
        self.ready.set()
        await asyncio.sleep(0)
        await r.round("controlled")
        r.snapshot.reset_mock()
        # Same entry, but sync/cache invalidation discovered an in-epoch reorg.
        r.cli.call.return_value={"generation":1,"namespace":"test"}
        self.ready.clear()
        await r.round("controlled")
        r.snapshot.assert_not_awaited()
        self.assertIsNone(r.prewarm_ready)
        self.assertFalse(r.prewarm_task.done())

    async def test_failed_prewarm_fails_closed_without_payments(self):
        async def fail():
            raise RuntimeError("invalid proof")
        self.runner.prewarm=fail
        await self.runner.round("controlled")
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError,"invalid proof"):
            await self.runner.round("controlled")
        self.runner.snapshot.assert_not_awaited()

    async def test_deferred_payment_is_submitted_once_after_prewarm(self):
        r=self.runner
        r.recover=Runner.recover.__get__(r)
        rid=r.journal.new_round(self.node.tip(),self.node.height,"controlled",1)
        operation=r.journal.plan(rid,"sim-001",None,["send","--amount-zat","10"])
        created={}
        async def cli(wallet,*args):
            if args[0] == "evidence-generation":
                return {"generation":0,"namespace":"test"}
            if args[0] == "operation":
                if args[1] not in created:
                    raise CliError("unknown operation ID")
                return created[args[1]]
            if args[0] == "send":
                op_id=args[args.index("--operation-id")+1]
                self.assertNotIn(op_id,created)
                created[op_id]={"txid":"payment-1","status":"pending"}
                self.node.mempool.append("payment-1")
                return {}
            raise AssertionError(args)
        r.cli.call=cli
        async def snapshot():
            r.wallet_tips["sim-001"]=(self.node.height,self.node.tip())
            return {}
        r.snapshot=snapshot
        for _ in range(10):
            await r.round("controlled")
        self.assertEqual(created,{})
        self.ready.set()
        await asyncio.sleep(0)
        await r.round("controlled")
        self.assertEqual(list(created),[operation["id"]])
        self.assertEqual(self.node.blocks[self.node.tip()]["tx"],["coinbase","payment-1"])
        self.assertEqual(r.journal.active_rounds(),[])
        self.assertEqual(r.journal.db.execute("SELECT count(*) FROM operations").fetchone()[0],1)

    async def test_normal_epoch_advance_keeps_existing_build(self):
        await self.runner.round("controlled")
        task=self.runner.prewarm_task
        self.node.height+=4096
        await self.runner.round("controlled")
        self.assertIs(task,self.runner.prewarm_task)
        self.assertFalse(task.cancelled())
        self.ready.set()
        await asyncio.sleep(0)
        await self.runner.round("controlled")
        self.assertIsNot(task,self.runner.prewarm_task)
        self.runner.snapshot.assert_not_awaited()

    async def test_prewarm_generate_timeout_reconciles_before_next_request(self):
        r=self.runner
        original=self.node.call
        failed=False
        async def timeout_once(method,params=None):
            nonlocal failed
            result=await original(method,params)
            if method == "generate" and not failed:
                failed=True
                raise TimeoutError("lost response")
            return result
        r.rpc.call=timeout_once
        with self.assertRaises(TimeoutError):
            await r.round("controlled")
        self.assertEqual(self.node.height,4105)
        self.assertEqual(r.journal.active_rounds()[0]["mining_requested"],1)
        await r.round("controlled")
        self.assertEqual(self.node.height,4106)
        self.assertEqual(self.node.calls.count("generate"),2)
        self.assertEqual(r.journal.active_rounds(),[])

    async def test_background_proof_slots_do_not_block_wallet_recovery(self):
        cli=Devtool(self.runner.config)
        gate=asyncio.Event()
        started=asyncio.Event()
        active=0
        maximum=0
        calls=0
        async def run(wallet,args):
            nonlocal active,maximum,calls
            if args[0] != "prewarm-epochs":
                return {"recovered":True}
            active+=1
            calls+=1
            maximum=max(maximum,active)
            if active == 4:
                started.set()
            await gate.wait()
            active-=1
            return {"profiles":7}
        cli._run=run
        tasks=[asyncio.create_task(cli.prewarm(f"sim-{i:03}")) for i in range(17)]
        await started.wait()
        self.assertEqual(await asyncio.wait_for(cli.call("sim-000","operation","id"),1), {"recovered":True})
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(calls,17)
        self.assertEqual(maximum,4)

    async def test_all_wallet_reorgs_finish_before_any_profile_build(self):
        r=self.runner
        for wallet in r.wallets:
            r.journal.reconciled(wallet)
        synced=set()
        async def sync(wallet):
            await asyncio.sleep(0)
            synced.add(wallet)
            return {"reorg":wallet == "sim-001"}
        async def prewarm(wallet):
            self.assertEqual(synced,set(r.wallets))
            return {"profiles":7,"through_epoch":0,"generation":2,"namespace":"test"}
        r.cli.sync=sync
        r.cli.prewarm=prewarm
        results=await Runner.prewarm(r)
        self.assertEqual(len(results),17)
        self.assertTrue(r.journal.needs_reconcile("sim-001"))
        self.assertFalse(r.journal.needs_reconcile("treasury"))

    async def test_background_reorg_reconciles_previously_mined_operations(self):
        r=self.runner
        r.wallets=["sim-001"]
        rid=r.journal.new_round("previous",1,"controlled",1)
        operation=r.journal.plan(rid,"sim-001",None,["send","--amount-zat","10"])
        r.journal.update(operation["id"],status="mined",txid="old-tx")
        r.journal.finish(rid,"old-block")
        r.journal.require_reconcile("sim-001")
        r.cli.sync.return_value={"height":self.node.height,"hash":self.node.tip(),"reorg":False}
        async def call(wallet,*args):
            if args[0] == "balance-summary":
                return {"available_zat":100,"transparent":None,"notes":[]}
            self.assertEqual(args,("transactions",))
            return {"transactions":[{"operation_id":operation["id"],"status":"pending","txid":"old-tx"}]}
        r.cli.call=call
        await Runner.snapshot(r)
        self.assertEqual(r.journal.get(operation["id"])["status"],"pending")
        self.assertFalse(r.journal.needs_reconcile("sim-001"))

    async def test_paginated_sync_preserves_reorg_notification(self):
        cli=Devtool(self.runner.config)
        cli.call=AsyncMock(side_effect=[{"caught_up":False,"reorg":True},{"caught_up":True,"reorg":False}])
        self.assertTrue((await cli.sync("sim-001"))["reorg"])

    async def test_known_inflight_invalidation_restarts_only_after_generation_changes(self):
        r=self.runner
        failed=asyncio.Event()
        async def invalidated():
            await failed.wait()
            raise CliError("epoch evidence invalidated by reorg")
        r.prewarm=invalidated
        await r.round("controlled")
        failed.set()
        await asyncio.sleep(0)
        r.cli.call.return_value={"generation":1,"namespace":"test"}
        await r.round("controlled")
        self.assertEqual(self.node.height,4106)
        self.assertEqual(r.prewarm_token,("test",1))
        r.snapshot.assert_not_awaited()
        # Repeating the same failure with an unchanged generation must stop;
        # evidence of a reorg is required to repeat this work.
        await asyncio.sleep(0)
        with self.assertRaisesRegex(CliError,"invalidated"):
            await r.round("controlled")

    async def test_dependency_namespace_change_requires_prewarm_again(self):
        r=self.runner
        await r.round("controlled")
        self.ready.set()
        await asyncio.sleep(0)
        await r.round("controlled")
        self.ready.clear()
        r.snapshot.reset_mock()
        r.cli.call.return_value={"generation":0,"namespace":"new-pinned-revision"}
        await r.round("controlled")
        r.snapshot.assert_not_awaited()
        self.assertIsNone(r.prewarm_ready)
        self.assertEqual(r.prewarm_token,("new-pinned-revision",0))
