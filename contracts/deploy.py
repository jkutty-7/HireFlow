"""
Deploy HireFlow Vyper contracts to Arc testnet.

Deploy order (dependencies):
  1. AgentRegistry.vy   — standalone
  2. PaymentEscrow.vy   — needs USDC address
  3. PaymentRouter.vy   — needs Registry + Escrow addresses

After deployment, copy the printed addresses to your .env file:
  AGENT_REGISTRY_ADDRESS=0x...
  PAYMENT_ESCROW_ADDRESS=0x...
  PAYMENT_ROUTER_ADDRESS=0x...

Usage:
  cd hireflow
  python contracts/deploy.py
"""

import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from web3 import Web3
from eth_account import Account
import vyper
from vyper.compiler import compile_code

from settings import settings


def compile_contract(path: str) -> dict:
    """Compile a Vyper contract and return ABI + bytecode."""
    with open(path) as f:
        source = f.read()
    output = compile_code(
        source,
        output_formats=["abi", "bytecode"],
    )
    return output


def deploy_contract(
    w3: Web3,
    abi: list,
    bytecode: str,
    deployer_account,
    constructor_args: list | None = None,
) -> str:
    """Deploy a contract and return its deployed address."""
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    constructor_args = constructor_args or []

    tx = contract.constructor(*constructor_args).build_transaction({
        "from":     deployer_account.address,
        "nonce":    w3.eth.get_transaction_count(deployer_account.address),
        "gas":      3_000_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed = w3.eth.account.sign_transaction(tx, private_key=deployer_account.key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Waiting for tx {tx_hash.hex()} ...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return receipt["contractAddress"]


def main():
    # ── Connect to Arc testnet ──────────────────────────────────────────────
    w3 = Web3(Web3.HTTPProvider(settings.arc_rpc_url))
    if not w3.is_connected():
        print(f"ERROR: Cannot connect to Arc RPC at {settings.arc_rpc_url}")
        sys.exit(1)
    print(f"Connected to Arc testnet (chain id {w3.eth.chain_id})")

    # ── Load deployer account ───────────────────────────────────────────────
    deployer_key = settings.orchestrator_private_key
    if not deployer_key or deployer_key == "0x":
        print("ERROR: Set ORCHESTRATOR_PRIVATE_KEY in .env before deploying")
        sys.exit(1)
    deployer = Account.from_key(deployer_key)
    print(f"Deployer: {deployer.address}")
    print(f"Balance:  {w3.from_wei(w3.eth.get_balance(deployer.address), 'ether')} USDC-gas")

    contracts_dir = os.path.dirname(__file__)

    # ── 1. Deploy AgentRegistry ─────────────────────────────────────────────
    print("\n[1/3] Compiling + deploying AgentRegistry.vy ...")
    registry_compiled = compile_contract(f"{contracts_dir}/AgentRegistry.vy")
    registry_address = deploy_contract(
        w3,
        abi=registry_compiled["abi"],
        bytecode=registry_compiled["bytecode"],
        deployer_account=deployer,
    )
    print(f"  AgentRegistry deployed at: {registry_address}")

    # ── 2. Deploy PaymentEscrow ─────────────────────────────────────────────
    print("\n[2/3] Compiling + deploying PaymentEscrow.vy ...")
    escrow_compiled = compile_contract(f"{contracts_dir}/PaymentEscrow.vy")
    # PaymentRouter address not known yet — we'll update it after step 3
    # For now, pass a placeholder; owner will call set_payment_router()
    escrow_address = deploy_contract(
        w3,
        abi=escrow_compiled["abi"],
        bytecode=escrow_compiled["bytecode"],
        deployer_account=deployer,
        constructor_args=[
            Web3.to_checksum_address(settings.usdc_contract_address),
            deployer.address,   # temporary router = deployer, updated below
        ],
    )
    print(f"  PaymentEscrow deployed at: {escrow_address}")

    # ── 3. Deploy PaymentRouter ─────────────────────────────────────────────
    print("\n[3/3] Compiling + deploying PaymentRouter.vy ...")
    router_compiled = compile_contract(f"{contracts_dir}/PaymentRouter.vy")
    router_address = deploy_contract(
        w3,
        abi=router_compiled["abi"],
        bytecode=router_compiled["bytecode"],
        deployer_account=deployer,
        constructor_args=[
            Web3.to_checksum_address(registry_address),
            Web3.to_checksum_address(escrow_address),
        ],
    )
    print(f"  PaymentRouter deployed at: {router_address}")

    # ── 4. Update PaymentEscrow to point to real router ─────────────────────
    print("\nUpdating PaymentEscrow.payment_router → PaymentRouter ...")
    escrow_contract = w3.eth.contract(
        address=Web3.to_checksum_address(escrow_address),
        abi=escrow_compiled["abi"],
    )
    tx = escrow_contract.functions.set_payment_router(
        Web3.to_checksum_address(router_address)
    ).build_transaction({
        "from":     deployer.address,
        "nonce":    w3.eth.get_transaction_count(deployer.address),
        "gas":      100_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed = w3.eth.account.sign_transaction(tx, private_key=deployer.key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"  Updated. tx: {tx_hash.hex()}")

    # ── Save ABIs for Python backend ─────────────────────────────────────────
    abi_dir = os.path.join(contracts_dir, "abis")
    os.makedirs(abi_dir, exist_ok=True)
    for name, compiled in [
        ("AgentRegistry", registry_compiled),
        ("PaymentEscrow", escrow_compiled),
        ("PaymentRouter", router_compiled),
    ]:
        with open(f"{abi_dir}/{name}.json", "w") as f:
            json.dump(compiled["abi"], f, indent=2)

    # ── Print env vars to copy ───────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Deployment complete! Add to your .env:")
    print("=" * 60)
    print(f"AGENT_REGISTRY_ADDRESS={registry_address}")
    print(f"PAYMENT_ESCROW_ADDRESS={escrow_address}")
    print(f"PAYMENT_ROUTER_ADDRESS={router_address}")
    print("=" * 60)


if __name__ == "__main__":
    main()
