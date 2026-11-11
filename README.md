# Tachyon workload sidecar

The Python 3.11+ Unix sidecar drives the existing JSON `tachyon` CLI. No Python
packages are required. Devtool retains all keys, signing, proof construction,
input reservations, and chain validation. This is **private testnet tooling with
mock proofs and unencrypted keys**, not a production wallet or proof service.

## Setup

The tested devtool revision is recorded in `DEVTOOL_REVISION`. Build its
`tachyon` feature as described in the
[wallet guide](https://github.com/S1nus/zcash-devtool/blob/d651c3cc3c9abeef071936fa708228ebc71aee20/doc/tachyon.md).
The sidecar invokes the resulting binary; it does not import or modify devtool.
Use Rust 1.97.1 for that build:

```sh
cd ../zcash-devtool
ROCKSDB_LIB_DIR="/opt/homebrew/lib" CARGO_INCREMENTAL=0 \
  RUSTFLAGS='--cfg zcash_unstable="nutachyon"' \
  cargo build --release --locked --features tachyon
cd ../tachyon-workload
cp config.example.toml config.local.toml
```

Edit `config.local.toml` to set your RPC URL and paths. The example assumes a
sibling `zcash-devtool` checkout and stores private runtime data in a separate
sibling `tachyon-workload-data` directory, outside either source checkout.
Relative paths resolve against the config file, not the working directory.
Use a trusted RPC, preferably through an SSH tunnel. `cookie_file` is optional.

```sh
python3 -m tachyon_workload --config config.local.toml \
  --accept-test-funds init
```

This reuses `treasury` within `wallet_root`, or creates it if missing, and creates
the numbered
`sim-001` through `sim-032` wallets. It prints the treasury's `miner_address`.
Existing wallets must match the RPC's genesis and NuTachyon activation block.
Configure the node with that address, `internal_miner = true`, and
**`tachyon_workload = false`**; the old node-side output generator replaces normal
mempool selection. Wait 100 confirmations for rewards. Do not reset the chain.

For a small smoke run, set `wallet_count = 4`, `max_transactions = 4`. Wallets
sync independently from activation; their initial synchronization over a remote
RPC can take several minutes. `concurrency` bounds simultaneous CLI commands.

```sh
# One funding payment per observed tip: full coinbase shielding, then note distribution.
python3 -m tachyon_workload --config config.local.toml --accept-test-funds \
  bootstrap --rounds 20
# Read-only chain activity: sync and report both pools without sending anything.
python3 -m tachyon_workload --config config.local.toml --accept-test-funds status
# Draw a fresh transaction budget for each observed tip. Default is ONE round.
python3 -m tachyon_workload --config config.local.toml --accept-test-funds \
  run --rounds 10
# Explicitly opt into a continuous process; Ctrl-C preserves recovery state.
python3 -m tachyon_workload --config config.local.toml --accept-test-funds \
  run --rounds 0
```

Bootstrap does not invent funds: it shields an entire mature reward minus the
50,000-zatoshi fee, then distributes differently sized Tachyon notes. Regular
rounds also refill underfunded wallets, **within** their budget. The default mix
is 80% intra-Tachyon, 8% unshield, 8% shield, and 4% transparent transfer, weighted
among currently affordable choices. Actual proportions differ during bootstrap
and when some wallets lack notes or transparent UTXOs. Receipt delivery/import
is automatic and private; there is no trial decryption or in-band secret delivery.

Each regular round draws uniformly from **zero through `max_transactions`**.
This counts all non-coinbase payments, including funding and transparent-only
payments, not only Tachyon transactions. Zero produces no new payment. A wallet
sends at most once per round, using confirmed inputs; outstanding transactions
block that wallet until resolved. Fees are additional to recipient amounts.
Unshielded funds can circulate transparently and then be reshielded.

## Mining and what the counts mean

**Live mode** works with your existing internal miner. Its budget is per
*observed tip*, not an exact transaction count in every mined block. Mining can
race construction, some tips can be skipped while syncing, and pending traffic
can carry into later blocks. A zero round does not promise a coinbase-only block.
Node-side aggregation remains Zakura's responsibility.

For deterministic batches on a PoW-disabled custom network, turn off
`internal_miner` and all other miners/producers, keep `tachyon_workload = false`,
and use:

```sh
python3 -m tachyon_workload --config config.local.toml --accept-test-funds \
  run --mode controlled --sole-miner --rounds 10
```

Controlled mode requires an empty mempool before each new round, compares the
batch against the mempool, calls `generate(1)`, then verifies the actual block's
transaction IDs. A zero round gives a coinbase-only block under those conditions.
Insufficient independent inputs or an epoch boundary produces an explicit
`underfilled` event, not a false claim of hitting the sampled target. Concurrent
miners or unrelated mempool traffic fail closed. An ambiguous `generate` timeout
is recovered by inspecting the persisted round's successor before mining again.
If no successor is visible yet, it stops rather than guessing whether the server
is still processing the first request. Investigate the node before resuming.

To share controlled mining with a public faucet or personal wallets, set
`allow_external_transactions = true` at the top level of the config (before
`[weights]` or `[faucet]`). It permits outside mempool transactions while still
requiring every transaction from the workload round to appear in its block.
Keep the workload as the sole miner. The sampled target now counts only workload
transactions; a zero round can mine faucet payments. Logs report the extra count
as `external_transactions`. This option defaults to `false`; live mode already
allows other traffic. Restart the service after changing the config.

The cap is a **load limit**, not the consensus maximum: block capacity depends on
serialized size, actions, and validation cost. Start small. Devtool reconstructs
proofs from local public inputs and routes closed epochs into buckets bounded by
Tachyon's polynomial capacity. The OSS removes that finalization work from
normal payments; it does not increase any protocol or wallet capacity limits.

## Oblivious Synchronization Service

Wallets get closed-epoch proof evidence from a `tachyon-oss` service instead of
building it themselves. Set `oss_url` (required; the OSS listens on
`127.0.0.1:8240` by default) and, if the OSS uses cookie auth, `oss_cookie_file`.
The sidecar passes them to every devtool command as `TACHYON_OSS_URL` and
`TACHYON_OSS_COOKIE`. Use a devtool build with OSS support.

Before planning new payments in epoch `e > 0`, the sidecar calls `oss_info` and
requires `evidenceReadyThrough >= e - 1`. Until then it logs `oss_not_ready` and
plans nothing; controlled mode keeps mining, so these blocks are empty unless
earlier broadcasts or permitted external transactions are waiting. If devtool
gives up with `OSS not ready`, the payment logs `payment_deferred` and keeps its
journaled ID and arguments. Its round is neither finished nor mined, and the next
round retries it. An unreachable OSS is an error, like an unreachable node.

Stop the previous release only between rounds, not while `epoch_prewarm` is
running: the journal refuses to open with unfinished prewarm rounds.

## Optional external faucet

Add this section to your workload config, replacing the address with a testnet
transparent P2PKH or P2SH address from a separate wallet you control:

```toml
[faucet]
address = "YOUR_TESTNET_TRANSPARENT_ADDRESS"
reward_share_bps = 1000
payout_interval_blocks = 10
min_payout_zat = 100000
```

This allocates **10% of each new coinbase reward paid to the treasury**, including
collected fees but excluding outputs paid to other addresses. Amounts use integer
zatoshis: `floor(reward_zat * reward_share_bps / 10000)` per block. The percentage
range is 1–9999 basis points; treasury pays transaction fees separately. The
faucet's keys never enter the sidecar, and it is never scheduled as a sender.

The first invocation with this configuration records a starting
height: the next block after the observed tip. It does **not** allocate your old
treasury balance or historical rewards. Keep the existing journal when restarting;
the stored height lets the sidecar account for every subsequent block, including
blocks mined while it was offline. Accounting scans at most 1,000 blocks per round
and defers new workload payments until it catches up to the treasury's synced tip.
It reads `getblock` and verbose `getrawtransaction` with an explicit block hash;
it requires integer `valueZat` outputs and an archive node, not an unspent-only view.

Rewards mature after **100 confirmations**. With a batch size of 10, the first
batch becomes payable when all ten blocks have matured. Eligible balances can
be paid over several rounds: devtool spends one note at a time. Mature coinbases
are shielded in full minus the fee, then Tachyon funds are unshielded to the faucet.
If a suitable treasury note already exists, it can be used directly. No manual
note receipt import is needed for the recipient; sync its transparent balance.

Faucet payouts and their shielding transactions count against the existing
`max_transactions` budget in both live and controlled mode. A sampled zero still
creates no transactions. Due faucet work gets the treasury slot before simulator
top-ups; only one treasury operation can be outstanding. Mature unpaid allocations
and a payout fee are protected from simulator funding, including between batches.
Payments below `min_payout_zat` remain owed (the minimum configurable threshold is
50,000 zatoshis). This is per-reward **accounting**, not a promise of a faucet output
in every block; insufficient liquidity, budget, or an epoch boundary can delay it.

`status` and regular round logs include a `faucet` event with each policy's:

- `immature_zat`: allocated but not mature;
- `owed_zat`: mature, unpaid, and not already assigned to an in-flight payout;
- `due_zat`: the portion of that debt eligible under the batch schedule;
- `in_flight_zat` and `paid_zat`: journaled payouts and confirmed payouts;
- `credit_zat`: excess payments after a reorg, offset against future allocations
  to the same recipient, including after rate changes.

`faucet_payment` events distinguish `replenish` from `payout`. Uncertain broadcasts
retain their signed transaction and operation ID; a retry never invents another
payment. Reorgs reconcile both reward allocations and payment status.

Changing the address, share, or batching settings creates a new policy **after
catching up under the old one**. It applies from the next block, not retroactively.
Old allocations and pending payouts retain their original destination and settings;
old policies' final partial batches become eligible once mature. Sub-threshold
remainders remain visible as owed. Removing `[faucet]` stops new allocations and
new faucet payments after catch-up, but recovery of already-planned payments still
runs, and old debt stays protected and journaled. Re-enable it to settle that debt.

No Zakura/devtool changes, wallet reset, miner-address change, or chain reset are
needed. Restart the sidecar after editing its config. Do not create a new journal
to change settings or retry a payment.

## Durability and operations

The mode-0700 state directory contains a SQLite journal and confidential receipt
files. A process lock prevents two sidecars using the same journal. Each payment's
ID, arguments, sender and recipient are committed **before** invoking devtool.
Retries recover the same signed transaction; they never silently create a new ID.
Receipts use a staging file and atomic rename; imports are idempotent. Wallet sync
refreshes transaction states, including mined payments rolled back by reorgs.
Expired transactions are not blindly replaced. Errors stop the process with
state intact; fix the cause and rerun using the same config and journal.

Back up **both the complete wallet directories and the sidecar state**. Never
commit, log, or publicly serve receipts or SQLite files. The default paths are
outside the repository. Do not operate these wallets concurrently with another
sidecar/journal or manually spend their inputs. Do not reuse them after regenesis.
Config must retain wallets needed by pending payments. Increasing `wallet_count`
and rerunning `init` safely adds wallets. The scheduler's `--seed` affects only
traffic choices, never key generation; exact replay uses the journal, not a seed.

For systemd, run as the user owning the private directories, set `UMask=0077`, use
the repository as `WorkingDirectory`, and set `ExecStart` to the continuous command
with absolute Python/config paths. If using filesystem hardening, make only the
wallet/state paths writable. Run `init` first; do not install a restart loop until
the bounded smoke run succeeds.

### Existing wallets from the devtool checkout

Moving the sidecar code does not require moving or reinitializing wallets. Point
`devtool`, `wallet_root`, and `state_dir` in your ignored `config.local.toml` to
their existing absolute paths. Preserve the original `wallet_root` spelling:
the journal pins it as well as chain identity and wallet addresses. Do not create
a fresh journal for wallets with in-flight operations.

Alternatively, keep using the original config file; paths still resolve against
that file's directory. Replace the old `python3 -m tools.tachyon_workload` command
with `python3 -m tachyon_workload` and run it from this repository. No chain reset
or miner-address change is needed.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s tachyon_workload -t . -v
```

Tests cover weighted selection, maturity, coinbase constraints, epoch boundaries,
zero rounds, private directories, journal locking and chain identity, reorg
recovery, ambiguous broadcasting/receipt delivery, and mining timeout recovery.
Faucet tests additionally cover maturity, exact accounting, batching, policy
changes, reward/payment reorgs, protected funds, and controlled-mode inclusion.
