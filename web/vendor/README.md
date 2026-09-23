# Third-party front-end components

| Component | Version | File | Source | License |
|---|---|---|---|---|
| vis-network (standalone UMD build, includes vis-data and vis-util) | 10.1.2 | `vis-network.min.js` | https://unpkg.com/vis-network@10.1.2/standalone/umd/vis-network.min.js | Dual-licensed: Apache-2.0 (`vis-network.LICENSE-APACHE-2.0.txt`) or MIT (`vis-network.LICENSE-MIT.txt`) |

The page loads the local copy first so the map works without network access;
`index.html` falls back to the same pinned version on unpkg only if the local
file is missing. No other third-party front-end code is used.
