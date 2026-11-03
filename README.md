# GFS QA logs extractor

One Python script and one configuration file. Requires Python 3.10 or newer;
uses only built-in libraries. No package installation or virtual environment is needed.

## Run

From this folder in a terminal:

```powershell
python qa_logs.py --limit 5
```

The script logs in automatically using `username` and `password` from `config.json`.
Check the sample CSV, then run the full export:

```powershell
python qa_logs.py
```

## Change the GFS target

Edit `config.json` before running. It describes one target; there are no project profiles.

| Setting | Purpose |
| --- | --- |
| `gfs_base_url` | GFS REST service URL, ending before `get-folder` |
| `auth_token_url` | Complete Keycloak token endpoint, including its realm |
| `client_id` | Keycloak client ID |
| `username`, `password` | GFS account credentials used for automatic login |
| `authorization_prefix` | Empty string matches the existing NAP tools; use `"Bearer "` if the target requires it |
| `root_paths` | Folder paths to crawl, including all their subfolders |
| `output_directory` | Output folder, relative to the config file unless absolute |
| `log_directory` | Log folder, relative to the config file unless absolute; defaults to `logs` |
| `output_name` | Filename prefix |
| `fields` | CSV column names and their exact JSON key paths |
| `required_fields` | Fields whose values must be nonempty |
| `timeout_seconds`, `max_attempts` | Request timeout and maximum attempts |

For Manila on the same NAP server, change `root_paths` to `["/NAP Manila"]`,
change `output_name` to `"NAP Manila"`, and change `username` and `password` to that account's credentials.
For another GFS deployment, also change the service/authentication URLs and client ID.
Authentication follows the Keycloak username/password flow used by the original tools.

`fields` maps `elementid` to `["extraData", "elementid"]`, meaning
`extraData.elementid`. CSV column order follows the order in this object.
The detail response's `fileName` is normalized to `name` automatically.

An alternative configuration can be selected with `--config path\to\config.json`.

## Output and errors

Each run creates a timestamped CSV in `output/` and a matching log in `logs/` by default.
Filename timestamps use UTC+8. CSVs use UTF-8 with a BOM for Excel, preserve
remarks/description text, and quote commas and embedded newlines correctly.

The script reads `get-folder` first. If a configured field is absent or a required
field is empty, it calls `get-file-details` for that file with `forRecent: false`.
Empty remarks and descriptions are valid. PDFs and audit histories are not downloaded.

Missing metadata and failed detail lookups are logged; available values are still
exported. Failed folders are logged while other folders continue. Folder-count
discrepancies produce warnings because those counts may be stale or filtered.
An explicit pagination flag, continuation value or larger total count stops that folder with an error rather than
silently claiming the listing is complete; pagination parameters are unverified.

Transient connection failures, HTTP 429 and HTTP 5xx responses are retried.
Server `Retry-After` delays are respected. Redirects are rejected; use the final API URLs.
Expired tokens are renewed. Persistent authentication failures stop the run.
Ctrl+C closes the CSV and saves rows already written. Each new run starts a fresh export.

Exit codes: `0` finished (including a deliberate sample limit; check warnings),
`1` configuration/file error, `2` export errors, `3` authentication failed,
`130` interrupted.
