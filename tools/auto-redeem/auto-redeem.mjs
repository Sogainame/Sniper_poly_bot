/**
 * Auto-redeem winning Polymarket positions via Safe/Proxy relayer.
 *
 * Uses @polymarket/builder-relayer-client for gasless redeem through
 * Polymarket's relayer. Does NOT call redeemPositions() directly from EOA.
 *
 * Usage:
 *   node auto-redeem.mjs --once   # single pass
 *   node auto-redeem.mjs          # continuous loop
 */

import { readFileSync, writeFileSync, existsSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import dotenv from "dotenv";
import { ethers } from "ethers";

const __dirname = dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: resolve(__dirname, ".env") });

// ── Config ──────────────────────────────────────────────────────────
const PRIVATE_KEY = process.env.PRIVATE_KEY;
const PROXY_WALLET = process.env.PROXY_WALLET;
const RPC_URL = process.env.RPC_URL || "https://polygon-rpc.com";
const CHAIN_ID = parseInt(process.env.CHAIN_ID || "137", 10);
const BUILDER_API_KEY = process.env.BUILDER_API_KEY;
const BUILDER_SECRET = process.env.BUILDER_SECRET;
const BUILDER_PASSPHRASE = process.env.BUILDER_PASSPHRASE;
const WALLET_TYPE = (process.env.WALLET_TYPE || "SAFE").toUpperCase();
const POLL_INTERVAL_SEC = parseInt(process.env.POLL_INTERVAL_SEC || "60", 10);
const DRY_RUN = (process.env.DRY_RUN || "true").toLowerCase() === "true";
const LOG_LEVEL = process.env.LOG_LEVEL || "info";

const DATA_API = "https://data-api.polymarket.com";
const STATE_PATH = resolve(__dirname, "redeem-state.json");
const PENDING_TIMEOUT_MS = 15 * 60 * 1000; // 15 minutes

// Polymarket CTF contract addresses (Polygon mainnet)
const CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
const USDCE_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";
const ZERO_BYTES32 = "0x" + "0".repeat(64);

// redeemPositions(address,bytes32,bytes32,uint256[])
const CTF_ABI = [
  "function redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)"
];

const ONCE_MODE = process.argv.includes("--once");

// ── Logging ─────────────────────────────────────────────────────────
function log(level, msg, data = {}) {
  const ts = new Date().toISOString();
  const prefix = `[${ts}] [${level.toUpperCase()}]`;
  const extra = Object.keys(data).length > 0 ? " " + JSON.stringify(data) : "";
  console.log(`${prefix} ${msg}${extra}`);
}

// ── State management ────────────────────────────────────────────────
function loadState() {
  try {
    if (existsSync(STATE_PATH)) {
      return JSON.parse(readFileSync(STATE_PATH, "utf-8"));
    }
  } catch (e) {
    log("warn", "Failed to load state, starting fresh", { error: e.message });
  }
  return { items: {} };
}

function saveState(state) {
  writeFileSync(STATE_PATH, JSON.stringify(state, null, 2), "utf-8");
}

function shouldSkip(state, conditionId) {
  const item = state.items[conditionId];
  if (!item) return false;
  if (item.status === "done") return true;
  if (item.status === "pending") {
    const elapsed = Date.now() - new Date(item.lastAttemptAt).getTime();
    return elapsed < PENDING_TIMEOUT_MS;
  }
  return false;
}

// ── Fetch redeemable positions ──────────────────────────────────────
async function fetchRedeemablePositions() {
  const url = `${DATA_API}/positions?user=${PROXY_WALLET}&redeemable=true&sizeThreshold=0`;
  log("info", `Fetching positions from ${url}`);

  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`Data API returned ${resp.status}: ${await resp.text()}`);
  }
  const positions = await resp.json();

  return positions.filter((p) => {
    const size = parseFloat(p.size || "0");
    const redeemable = p.redeemable === true;
    const hasCondition = !!p.conditionId;
    return redeemable && size > 0 && hasCondition;
  });
}

// ── Build redeem calldata ───────────────────────────────────────────
function buildRedeemCalldata(conditionId) {
  const iface = new ethers.Interface(CTF_ABI);
  const data = iface.encodeFunctionData("redeemPositions", [
    USDCE_ADDRESS,
    ZERO_BYTES32,
    conditionId,
    [1, 2],
  ]);
  return {
    to: CTF_ADDRESS,
    data,
    value: "0",
  };
}

// ── Init relayer client ─────────────────────────────────────────────
const RELAYER_URL = "https://builder-relayer.polymarket.com";

async function initRelayerClient() {
  const relayerModule = await import("@polymarket/builder-relayer-client");

  const RelayClient = relayerModule.RelayClient || relayerModule.default?.RelayClient;
  if (!RelayClient) {
    log("warn", "Available exports from builder-relayer-client", {
      keys: Object.keys(relayerModule),
    });
    throw new Error("Cannot find RelayClient in @polymarket/builder-relayer-client");
  }

  const RelayerTxType = relayerModule.RelayerTxType;

  // Constructor: RelayClient(relayerUrl, chainId, signer, builderConfig, relayTxType)
  const signer = PRIVATE_KEY;
  const builderConfig = {
    localBuilderCreds: {
      key: BUILDER_API_KEY,
      secret: BUILDER_SECRET,
      passphrase: BUILDER_PASSPHRASE,
    },
  };
  const txType = WALLET_TYPE === "PROXY" ? RelayerTxType.PROXY : RelayerTxType.SAFE;

  return new RelayClient(RELAYER_URL, CHAIN_ID, signer, builderConfig, txType);
}

// ── Main redeem loop ────────────────────────────────────────────────
async function redeemOnce() {
  const state = loadState();

  // 1. Fetch redeemable positions
  let positions;
  try {
    positions = await fetchRedeemablePositions();
  } catch (e) {
    log("error", "Failed to fetch positions", { error: e.message });
    return;
  }

  log("info", `Found ${positions.length} redeemable positions`);

  if (positions.length === 0) {
    return;
  }

  // 2. Filter already processed
  const toRedeem = positions.filter((p) => {
    if (!p.conditionId) return false;

    // Skip negative risk markets
    if (p.negativeRisk === true || p.neg_risk === true) {
      log("info", "skip_negative_risk", {
        conditionId: p.conditionId,
        outcome: p.outcome,
      });
      return false;
    }

    if (shouldSkip(state, p.conditionId)) {
      log("debug", "Skipping already processed", {
        conditionId: p.conditionId,
        status: state.items[p.conditionId]?.status,
      });
      return false;
    }
    return true;
  });

  log("info", `${toRedeem.length} positions to redeem after filtering`);

  if (toRedeem.length === 0) return;

  // 3. Init relayer (only if we have work to do)
  let client = null;
  if (!DRY_RUN) {
    try {
      client = await initRelayerClient();
      log("info", "Relayer client initialized", { walletType: WALLET_TYPE });
    } catch (e) {
      log("error", "Failed to init relayer client", { error: e.message });
      return;
    }
  }

  // 4. Process each position
  for (const pos of toRedeem) {
    const conditionId = pos.conditionId;
    const size = parseFloat(pos.size || "0");
    const outcome = pos.outcome || "unknown";

    const logData = {
      proxyWallet: PROXY_WALLET,
      conditionId,
      outcome,
      size,
      dryRun: DRY_RUN,
    };

    if (DRY_RUN) {
      log("info", "PLANNED redeem (dry run)", { ...logData, status: "planned" });
      state.items[conditionId] = {
        status: "planned",
        lastAttemptAt: new Date().toISOString(),
        txHash: null,
        error: null,
        outcome,
        size,
      };
      saveState(state);
      continue;
    }

    // Live redeem
    log("info", "Executing redeem", { ...logData, status: "pending" });
    state.items[conditionId] = {
      status: "pending",
      lastAttemptAt: new Date().toISOString(),
      txHash: null,
      error: null,
      outcome,
      size,
    };
    saveState(state);

    try {
      const redeemTx = buildRedeemCalldata(conditionId);
      let resp;
      if (WALLET_TYPE === "PROXY") {
        resp = await client.executeProxyTransactions([redeemTx]);
      } else {
        resp = await client.executeSafeTransactions([redeemTx]);
      }
      const result = typeof resp?.wait === "function" ? await resp.wait() : resp;

      const txHash = result?.transactionHash || result?.hash || result?.id || JSON.stringify(result);
      log("info", "Redeem SUCCESS", { ...logData, status: "success", txHash });

      state.items[conditionId] = {
        status: "done",
        lastAttemptAt: new Date().toISOString(),
        txHash,
        error: null,
        outcome,
        size,
      };
    } catch (e) {
      log("error", "Redeem FAILED", { ...logData, status: "failed", error: e.message });

      state.items[conditionId] = {
        status: "failed",
        lastAttemptAt: new Date().toISOString(),
        txHash: null,
        error: e.message,
        outcome,
        size,
      };
    }
    saveState(state);
  }
}

// ── Entry point ─────────────────────────────────────────────────────
async function main() {
  log("info", "Auto-redeem starting", {
    proxyWallet: PROXY_WALLET,
    walletType: WALLET_TYPE,
    dryRun: DRY_RUN,
    pollInterval: POLL_INTERVAL_SEC,
    onceMode: ONCE_MODE,
  });

  if (!PROXY_WALLET) {
    log("error", "PROXY_WALLET not set in .env");
    process.exit(1);
  }

  if (!DRY_RUN && (!BUILDER_API_KEY || !BUILDER_SECRET || !BUILDER_PASSPHRASE)) {
    log("error", "Builder credentials required for live mode. Set BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE");
    process.exit(1);
  }

  if (ONCE_MODE) {
    await redeemOnce();
    log("info", "Single pass complete, exiting");
    return;
  }

  // Continuous loop
  while (true) {
    try {
      await redeemOnce();
    } catch (e) {
      log("error", "Unhandled error in redeem loop", { error: e.message });
    }
    log("info", `Sleeping ${POLL_INTERVAL_SEC}s...`);
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_SEC * 1000));
  }
}

main().catch((e) => {
  log("error", "Fatal error", { error: e.message, stack: e.stack });
  process.exit(1);
});
