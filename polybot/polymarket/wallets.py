"""Derive the Polymarket account wallet ("funder") that belongs to a signer key.

Polymarket has had three wallet generations. Which one your account uses decides BOTH the funder
address and the signature_type the CLOB expects:

  type 0  EOA               the signer trades directly
  type 1  Proxy wallet      legacy Magic/Google login  (CREATE2 minimal proxy)
  type 2  Gnosis Safe       legacy external signer (MetaMask/Rabby)
  type 3  Deposit Wallet    the standard for accounts created from ~May 2026 (ERC1967 beacon proxy)

Only py-clob-client-v2 can sign for type 3.
"""
from __future__ import annotations
from typing import Dict
from eth_utils import keccak, to_checksum_address

PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
PROXY_INIT_CODE_HASH = bytes.fromhex("d21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b")
DEPOSIT_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
DEPOSIT_BEACON = "0x7A18EDfe055488A3128f01F563e5B479D92ffc3a"
# Solady LibClone.initCodeERC1967BeaconProxy(beacon, args) constant fragments
_C23 = bytes.fromhex("60195155f3363d3d373d3d363d602036600436635c60da")
_C32A = bytes.fromhex("1b60e01b36527fa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6c")
_C32B = bytes.fromhex("b3582b35133d50545afa5036515af43d6000803e604d573d6000fd5b3d6000f3")


def _create2(deployer: str, salt: bytes, init_code_hash: bytes) -> str:
    return to_checksum_address(keccak(b"\xff" + bytes.fromhex(deployer[2:]) + salt + init_code_hash)[12:])


def proxy_wallet(signer: str) -> str:
    """Legacy Magic/Google proxy (signature type 1)."""
    return _create2(PROXY_FACTORY, keccak(bytes.fromhex(signer[2:])), PROXY_INIT_CODE_HASH)


def deposit_wallet(signer: str) -> str:
    """Current Deposit Wallet (signature type 3): ERC1967 beacon proxy deployed by the factory."""
    args = bytes(12) + bytes.fromhex(DEPOSIT_FACTORY[2:]) + bytes(12) + bytes.fromhex(signer[2:])
    n = len(args)
    init_code = (b"\x61" + (n + 0x52).to_bytes(2, "big") + bytes.fromhex("3d8160233d3973")
                 + bytes.fromhex(DEPOSIT_BEACON[2:]) + _C23 + _C32A + _C32B + args)
    return _create2(DEPOSIT_FACTORY, keccak(args), keccak(init_code))


def candidates(signer: str) -> Dict[int, str]:
    """signature_type -> the funder address that type implies for this signer."""
    return {0: to_checksum_address(signer), 1: proxy_wallet(signer), 3: deposit_wallet(signer)}


def identify(signer: str, funder: str) -> tuple[int | None, str]:
    """Which signature_type does this (signer, funder) pair correspond to?"""
    for sig_type, addr in candidates(signer).items():
        if addr.lower() == funder.lower():
            names = {0: "EOA", 1: "legacy proxy wallet", 3: "Deposit Wallet"}
            return sig_type, names[sig_type]
    return None, ("no match - the funder is not derivable from this key "
                  "(a Gnosis Safe, or the key belongs to a different account)")
