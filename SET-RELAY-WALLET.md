# setRelayWallet — Register New Relay On-Chain

**After relay wallet rotation (2026-07-01).** Railway has the new key, but **uploads will fail** until each contract's `relayWallet` is updated on-chain.

## Wallets

| Role | Address |
|------|---------|
| **New relay** (register this) | `0xbb0ab4c9E15a20661CA0C4d2b6f5D32A7EdF7646` |
| **Old relay** (compromised — do not use) | `0xC19dc1E0d9d63E6E204207c9eFb53ec1F302015f` |
| **Signer** (contract owner) | MetaMask `0x6518fD26a7aD2Fe1bA80De5f279Ee59F55C0A9bA` |

## Contracts (call setRelayWallet on all four)

| App | Contract | Address |
|-----|----------|---------|
| LightTunes V1 | LightTunesV1 | `0x3587067a1E37A1c05095B3cc053564Db49a27F7D` |
| LightTube V2 | LightTubeV2 | `0xf8cdC4f6241D655bB4E30fF5f1433Fb9F358aDB5` |
| LightTube V3 | LightTubeV3 | `0x2077AbeBe64461f0265937328C9e710C35E18Fd3` |
| OrcaVault V3 | OrcaVaultV3 | `0x5262424DcF7e810575700a28C7Ec8A6d3dd4A0dA` |

## Method A — Remix (recommended)

1. MetaMask → **Lightchain mainnet** (chain ID **9200**), account `0x6518…`
2. Open https://remix.ethereum.org
3. For each contract, paste the `.sol` from the app folder (or use "At Address" with ABI):
   - `~/Desktop/lighttunes-app/LightTunesV1.sol`
   - `~/Desktop/lighttube-app/LightTubeV2.sol` / `LightTubeV3.sol`
   - `~/Desktop/orcavault-app/OrcaVaultV3.sol`
4. Compile 0.8.20 → Deploy tab → **At Address** → paste contract address → Connect
5. Expand contract → `setRelayWallet` → `_relay`:
   ```
   0xbb0ab4c9E15a20661CA0C4d2b6f5D32A7EdF7646
   ```
6. **Transact** → confirm in MetaMask → repeat for all 4 contracts

## Method B — Raw calldata (advanced)

Same calldata for every contract (`setRelayWallet(address)`):

```
0xb8b55471000000000000000000000000bb0ab4c9e15a20661ca0c4d2b6f5d32a7edf7646
```

## Verify after each tx

On https://mainnet.lightscan.app — read contract `relayWallet()`:

- Must return `0xbb0ab4c9E15a20661CA0C4d2b6f5D32A7EdF7646`
- If still `0xC19dc1…`, tx failed or wrong contract

## Test uploads

1. LightTunes — small test upload via https://lighttunes.win
2. LightTube — short test video via https://lighttube.win
3. OrcaVault — small memory via https://orcavault.win

Relay must have LCAI for gas (~200+ funded; 500+ recommended).