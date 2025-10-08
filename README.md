# Sui Wallet Holdings Viewer

This repository now hosts a single-page web application that inspects the
fungible token balances inside any Sui wallet. The app is **read only** and
runs entirely in the browser – it never signs or submits transactions. When you
enter a wallet address the page queries Sui's public JSON-RPC endpoint to list
all detected coin types with human readable balances and expandable activity
history for each token, complete with coin artwork when available.
all detected coin types with human readable balances.

## Running locally

Because the site is static you only need a web server to test it locally. Any
simple file server works:

```bash
python -m http.server 8000
```

Then open [http://localhost:8000](http://localhost:8000) in your browser and
load `index.html`.

## Deploying to the web from GitHub

The app is a static site, so you can publish it straight from your repository
using GitHub Pages:

1. Commit the project to a GitHub repository and push your branch.
2. In the GitHub UI, go to **Settings → Pages**.
3. Under **Build and deployment**, select **Deploy from a branch**.
4. Choose the branch you pushed (for example `main`) and the `/` (root)
   directory.
5. Save. GitHub will build the static site automatically and give you a public
   URL you can share to test the wallet reader in the browser.

Alternatively, you can deploy the same static files to services like Vercel,
Netlify, or Cloudflare Pages.

## How it works

* Submits `suix_getAllBalances` JSON-RPC requests to fetch all coin types owned
  by an address.
* Queries `suix_getCoinMetadata` for each coin type to resolve symbols, names,
  icons, and decimals.
* Pulls recent transactions that either send or receive assets for the wallet
  with `suix_queryTransactionBlocks`, groups the balance changes per coin type,
  sorts them by recency, and renders the latest entries in expandable panels
  beneath each token row.
* Converts the raw integer balances and balance deltas into human-friendly
  amounts using the metadata and displays the results in a responsive table.
* Queries `suix_getCoinMetadata` for each coin type to resolve symbols and
  decimals.
* Converts the raw integer balances into human-friendly amounts using the
  metadata and displays the results in a responsive table.

## Notes

* This is a client-side reader only; it does **not** connect to a wallet or
  request any signing permissions.
* The app requires the public Sui RPC endpoint to be reachable from the user's
  browser. If the endpoint enforces CORS or rate limits, you may need to host a
  proxy under your control.
* Each token activity panel shows up to the 10 most recent balance changes for
  that coin to keep the UI scannable.
