"""
set_allowances.py — Одноразовая настройка разрешений для Polymarket.

ЧТО ДЕЛАЕТ:
  Выставляет разрешения чтобы бот мог ПРОДАВАТЬ токены (не только покупать).
  Без этого — ошибка "not enough balance / allowance" при каждой продаже.
  
  Нужен POL (газ) на кошельке — буквально $0.01.
  
ЗАПУСК:
  python set_allowances.py
  
  Нужно запустить только ОДИН РАЗ. После этого продажа будет работать навсегда.

КАК РАБОТАЕТ:
  1. Разрешает USDC.e контракту Polymarket тратить ваши доллары (для покупок)
  2. Разрешает CTF контракту управлять вашими токенами (для продаж)
  3. Повторяет для всех трёх Exchange контрактов Polymarket
"""

import os
import json
import time
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
FUNDER = os.getenv("POLY_FUNDER_ADDRESS", "")

# Адреса контрактов Polymarket на Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"      # USDC.e
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"        # Conditional Tokens
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"      # CTF Exchange
NEG_RISK_CTF = "0xC5d563A36AE78145C45a50134d48A1215220f80a"       # Neg Risk CTF Exchange
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"   # Neg Risk Adapter

MAX_UINT256 = 2**256 - 1

# ABI для вызова approve (USDC) и setApprovalForAll (CTF/токены)
ERC20_APPROVE_ABI = json.loads('[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}]')
ERC1155_APPROVAL_ABI = json.loads('[{"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"}]')


def main():
    if not PRIVATE_KEY or not FUNDER:
        print("❌ Нет POLY_PRIVATE_KEY или POLY_FUNDER_ADDRESS в .env")
        return

    print(f"🔧 Настройка разрешений для Polymarket")
    print(f"   Кошелёк: {FUNDER}")
    print()

    # Сначала пробуем через py-clob-client (работает для proxy wallets)
    print("── Способ 1: через CLOB API (для proxy/email кошельков) ──")
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        client = ClobClient(
            host="https://clob.polymarket.com",
            key=PRIVATE_KEY,
            chain_id=137,
            signature_type=1,
            funder=FUNDER,
        )
        creds = client.create_or_derive_api_creds()
        if creds:
            client.set_api_creds(creds)

        # Обновить USDC allowance
        print("   📝 Обновляю USDC allowance...")
        try:
            resp = client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            print(f"   ✅ USDC: {resp}")
        except Exception as e:
            print(f"   ⚠ USDC: {e}")

        # Проверить текущий баланс
        try:
            bal = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            print(f"   💰 Баланс: {bal}")
        except Exception as e:
            print(f"   ⚠ Баланс: {e}")

        print("   ✅ CLOB API разрешения обновлены")
        print()
    except Exception as e:
        print(f"   ❌ CLOB API не сработал: {e}")
        print()

    # Способ 2: прямые on-chain транзакции (нужен POL для газа)
    print("── Способ 2: on-chain approve (нужен POL для газа) ──")
    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))

        if not w3.is_connected():
            print("   ❌ Не удалось подключиться к Polygon RPC")
            return

        # Проверяем баланс POL для газа
        account = w3.eth.account.from_key(PRIVATE_KEY)
        pub_key = account.address
        pol_balance = w3.eth.get_balance(pub_key)
        pol_in_ether = w3.from_wei(pol_balance, 'ether')
        print(f"   Аккаунт EOA: {pub_key}")
        print(f"   POL баланс: {pol_in_ether:.6f} POL")

        if pol_balance < w3.to_wei(0.001, 'ether'):
            print(f"   ⚠ Мало POL для газа. Нужно отправить POL на {pub_key}")
            print(f"   ⚠ (Это EOA адрес, не Polymarket proxy!)")
            print(f"   Polymarket proxy (funder): {FUNDER}")
            print()
            print(f"   Если POL отправлен на proxy ({FUNDER}),")
            print(f"   то on-chain approve не сможет его использовать.")
            print(f"   Для proxy wallet — разрешения ставятся через UI Polymarket")
            print(f"   при первой сделке, или через Способ 1 (CLOB API).")
            return

        # Контракты
        usdc = w3.eth.contract(address=w3.to_checksum_address(USDC_ADDRESS), abi=ERC20_APPROVE_ABI)
        ctf = w3.eth.contract(address=w3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_APPROVAL_ABI)

        # Список Exchange контрактов которым нужно дать разрешение
        exchanges = [
            ("CTF Exchange", CTF_EXCHANGE),
            ("Neg Risk CTF Exchange", NEG_RISK_CTF),
            ("Neg Risk Adapter", NEG_RISK_ADAPTER),
        ]

        nonce = w3.eth.get_transaction_count(pub_key)

        for name, addr in exchanges:
            addr_cs = w3.to_checksum_address(addr)

            # 1. Approve USDC.e
            print(f"   📝 USDC approve для {name}...")
            try:
                tx = usdc.functions.approve(addr_cs, MAX_UINT256).build_transaction({
                    'from': pub_key,
                    'nonce': nonce,
                    'gas': 60000,
                    'gasPrice': w3.to_wei(50, 'gwei'),
                    'chainId': 137,
                })
                signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                status = "✅" if receipt['status'] == 1 else "❌"
                print(f"   {status} USDC → {name}: {tx_hash.hex()}")
                nonce += 1
            except Exception as e:
                print(f"   ❌ USDC → {name}: {e}")

            # 2. setApprovalForAll CTF
            print(f"   📝 CTF setApprovalForAll для {name}...")
            try:
                tx = ctf.functions.setApprovalForAll(addr_cs, True).build_transaction({
                    'from': pub_key,
                    'nonce': nonce,
                    'gas': 60000,
                    'gasPrice': w3.to_wei(50, 'gwei'),
                    'chainId': 137,
                })
                signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                status = "✅" if receipt['status'] == 1 else "❌"
                print(f"   {status} CTF → {name}: {tx_hash.hex()}")
                nonce += 1
            except Exception as e:
                print(f"   ❌ CTF → {name}: {e}")

            time.sleep(1)

        print()
        print("✅ Все разрешения выставлены! Продажа токенов теперь будет работать.")
        print("   Этот скрипт больше запускать не нужно.")

    except ImportError:
        print("   ❌ Нужна библиотека web3: pip install web3")
    except Exception as e:
        print(f"   ❌ Ошибка: {e}")


if __name__ == "__main__":
    main()
