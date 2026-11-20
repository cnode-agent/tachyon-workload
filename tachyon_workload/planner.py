"""Balance-aware selection: a single confirmed input, at most one payment per wallet."""

FEE = 50_000


def available_notes(balance):
    return sorted(note["value_zat"] for note in balance["notes"] if note["status"] == "available")


def available_note_count(balance):
    return sum(1 for note in balance["notes"] if note["status"] == "available")


def consolidation_payment(wallet, max_inputs):
    """Merge the wallet's smallest notes (dust first) into one note."""
    return (wallet, None, ["consolidate", "--max-inputs", str(max_inputs)])


def funding_payment(config, balances, utxos, addresses, busy, protected_zat=0):
    if "treasury" in busy:
        return None
    needy = []
    for wallet, balance in balances.items():
        if wallet == "treasury":
            continue
        # Unconfirmed receipts can outlive an expired payment; don't count them
        # as funding. An in-flight treasury payment already makes it busy.
        count = sum(note["status"] in ("available", "reserved") and
                    note["value_zat"] >= config["min_amount_zat"] + FEE for note in balance["notes"])
        if count < config["seed_notes"]:
            needy.append((count, wallet))
    if not needy:
        return None
    count, recipient = min(needy)
    # Different denominations give the single-input wallet usable choices.
    amount = config["seed_amount_zat"] * (1 + count)
    notes = available_notes(balances["treasury"])
    if sum(notes) >= amount + FEE + protected_zat and any(note >= amount + FEE for note in notes):
        return ("treasury", recipient, ["send", "--to", addresses[recipient]["tachyon_address"],
                                         "--amount-zat", str(amount)])
    for utxo in utxos.get("treasury", []):
        if utxo["satoshis"] > amount + 2 * FEE:
            return ("treasury", None, ["shield", "--txid", utxo["txid"],
                                       "--index", str(utxo["outputIndex"])])
    return None


def choose_payment(config, rng, wallet, balances, utxos, addresses, tachyon_allowed):
    if wallet == "treasury":
        return None
    candidates = []
    notes = available_notes(balances[wallet])
    transparent = [u["satoshis"] for u in utxos.get(wallet, []) if not u["coinbase"]]
    limits = {"send": max(notes, default=0), "unshield": max(notes, default=0),
              "shield": max(transparent, default=0), "transparent-send": max(transparent, default=0)}
    for kind, value in limits.items():
        ceiling = min(config["max_amount_zat"], value - FEE)
        weight = config["weights"][kind]
        if ceiling >= config["min_amount_zat"] and weight and (tachyon_allowed or kind == "transparent-send"):
            candidates.append((kind, ceiling, weight))
    if not candidates:
        return None
    kind, ceiling, _ = rng.choices(candidates, weights=[item[2] for item in candidates])[0]
    amount = str(rng.randint(config["min_amount_zat"], ceiling))
    if kind == "shield":
        return (wallet, None, [kind, amount])
    recipient = rng.choice(sorted(name for name in balances if name not in (wallet, "treasury")))
    address = addresses[recipient]["tachyon_address" if kind == "send" else "funding_address"]
    return (wallet, recipient if kind == "send" else None, [kind, "--to", address, "--amount-zat", amount])
