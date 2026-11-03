"""Export GFS QA metadata using one configurable target. Python 3.10+, no dependencies."""

import argparse
import csv
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import logging
from math import isfinite
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # API URLs must be final; do not forward credentials on redirects.


HTTP = build_opener(NoRedirect())


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class AuthError(ApiError):
    pass


def request_json(method, url, config, data=None, headers=None):
    """Retry connection failures, rate limits and server errors; never log secrets."""
    for attempt in range(config["max_attempts"]):
        delay = 2 ** attempt
        try:
            request = Request(url, data=data, headers=headers or {}, method=method)
            with HTTP.open(request, timeout=config["timeout_seconds"]) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ApiError("Expected a JSON object from the server.")
            return result
        except HTTPError as error:
            status = error.code
            try:
                retry_after = error.headers.get("Retry-After", "0")
                wait = int(retry_after) if retry_after.isdecimal() else parsedate_to_datetime(retry_after).timestamp() - time.time()
                delay = max(delay, wait)
            except (AttributeError, TypeError, ValueError, OverflowError):
                pass
            error.close()
            if status != 429 and not 500 <= status < 600:
                raise ApiError(f"HTTP {status} from {urlsplit(url).path}.", status) from None
            failure = ApiError(f"HTTP {status} after {attempt + 1} attempt(s).", status)
        except (OSError, HTTPException):
            failure = ApiError(f"Connection failed after {attempt + 1} attempt(s).")
        except ValueError:
            raise ApiError("The server response is not valid JSON.") from None
        if attempt + 1 == config["max_attempts"]:
            raise failure
        time.sleep(delay)


class GfsClient:
    def __init__(self, config, username, password):
        self.config, self.username, self.password = config, username, password
        self.access_token, self.refresh_token, self.expires_at = None, None, 0

    def login(self):
        form = {"client_id": self.config["client_id"], "grant_type": "password",
                "username": self.username, "password": self.password}
        if self.refresh_token:
            form = {"client_id": self.config["client_id"], "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token}
        try:
            try:
                token = request_json("POST", self.config["auth_token_url"], self.config,
                                     urlencode(form).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
            except ApiError as error:
                if self.refresh_token and error.status in (400, 401):
                    self.refresh_token = None
                    return self.login()
                raise
            if not isinstance(token.get("access_token"), str) or not token["access_token"]:
                raise ApiError("Login returned no access token.")
            lifetime = float(token.get("expires_in", 60))
            if not isfinite(lifetime) or lifetime <= 0:
                raise ApiError("Login returned an invalid token lifetime.")
            refresh = token.get("refresh_token", self.refresh_token)
            if refresh is not None and not isinstance(refresh, str):
                raise ApiError("Login returned an invalid refresh token.")
            self.access_token = token["access_token"]
            self.refresh_token = refresh
            self.expires_at = time.monotonic() + lifetime - min(30, lifetime / 10)
        except AuthError:
            raise
        except (ApiError, ValueError, TypeError) as error:
            message = str(error) if isinstance(error, ApiError) else "Invalid token response."
            raise AuthError(f"Authentication failed: {message}") from None

    def call(self, endpoint, params=None, payload=None):
        url = self.config["gfs_base_url"].rstrip("/") + "/" + endpoint
        if params:
            url += "?" + urlencode(params)
        for renewal in range(2):
            if renewal or not self.access_token or time.monotonic() >= self.expires_at:
                self.login()
            headers = {"Authorization": self.config["authorization_prefix"] + self.access_token,
                       "Accept": "application/json"}
            data = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(payload).encode()
            try:
                return request_json("POST" if payload is not None else "GET", url, self.config, data, headers)
            except ApiError as error:
                if error.status != 401:
                    raise
                if renewal:
                    raise AuthError("GFS rejected authorization after token renewal (HTTP 401).") from None


MISSING = object()


def read_fields(item, fields):
    values = {}
    for column, keys in fields.items():
        value = item
        for key in keys:
            value = value.get(key, MISSING) if isinstance(value, dict) else MISSING
        values[column] = value
    return values


def missing_fields(values, required):
    return [name for name, value in values.items()
            if value is MISSING or (name in required and (value is None or not str(value).strip()))]


def crawl(client, config, writer, limit=None):
    folders = list(reversed(config["root_paths"]))
    seen_folders, seen_files = set(), set()
    rows = errors = warnings = 0
    while folders:
        path = folders.pop()
        if path in seen_folders:
            continue
        seen_folders.add(path)
        logging.info("Listing %s", path)
        try:
            listing = client.call("get-folder", params={"parentPath": path, "isSudo": "false"})
            items = listing.get("folderContent")
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise ApiError("Expected a folderContent list.")
            metadata = listing.get("folderData", {})
            if not isinstance(metadata, dict) or metadata.get("path") != path:
                raise ApiError("Response folder path does not match the request.")
            more = any(listing.get(key) is True for key in ("hasMore", "has_more"))
            more = more or any(listing.get(key) not in (None, "", False)
                               for key in ("nextPage", "next_page", "nextCursor", "next_cursor"))
            more = more or any(type(listing.get(key)) is int and listing[key] > len(items)
                               for key in ("total", "totalCount", "totalElements"))
            if more:
                raise ApiError("This response indicates pagination; its paging parameters need verification.")
            for key, count in (("files", sum(item.get("isFolder") is False for item in items)),
                               ("subfolders", sum(item.get("isFolder") is True for item in items))):
                if key in metadata and metadata[key] != count:
                    warnings += 1
                    logging.warning("%s: reported %s %s, received %s; completeness is unverified.",
                                    path, metadata[key], key, count)
        except AuthError:
            raise
        except ApiError as error:
            errors += 1
            logging.error("Cannot list %s: %s", path, error)
            continue
        for item in items:
            child = item.get("path")
            if (not isinstance(child, str) or child == path
                    or child.rsplit("/", 1)[-1] in ("", ".", "..")
                    or (child.rsplit("/", 1)[0] or "/") != path
                    or type(item.get("isFolder")) is not bool):
                errors += 1
                logging.error("Invalid entry in %s; entry skipped.", path)
                continue
            if item["isFolder"]:
                folders.append(child)
                continue
            file_id = item.get("fileId")
            if file_id is not None and not isinstance(file_id, str):
                errors += 1
                logging.error("Invalid fileId for %s; entry skipped.", child)
                continue
            identity = file_id or child
            if identity in seen_files:
                continue
            seen_files.add(identity)
            values = read_fields(item, config["fields"])
            missing = missing_fields(values, config["required_fields"])
            if missing:
                try:
                    if not file_id:
                        raise ApiError("No fileId available for the detail lookup.")
                    details = client.call("get-file-details", payload={"fileId": file_id,
                                          "search-string": f"file_id:{file_id}", "forRecent": False})
                    if details.get("path") != child or details.get("isFolder") is not False or "auditTrail" in details:
                        raise ApiError("Detail response does not match the file.")
                    details["name"] = details.get("fileName", item.get("name"))
                    for name, value in read_fields(details, config["fields"]).items():
                        if value is not MISSING:
                            values[name] = value
                    missing = missing_fields(values, config["required_fields"])
                except AuthError:
                    raise
                except ApiError as error:
                    errors += 1
                    logging.error("Detail lookup failed for %s: %s", child, error)
                    missing = []  # Already reported as a request failure.
            if missing:
                errors += 1
                logging.error("%s: missing metadata: %s", child, ", ".join(missing))
            writer.writerow({name: "" if value is MISSING or value is None else value for name, value in values.items()})
            rows += 1
            if rows % 100 == 0:
                print(f"Exported {rows} files...", flush=True)
            if limit and rows >= limit:
                return rows, errors, warnings, "Sample limit reached"
    return rows, errors, warnings, "Finished"


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object.")
    config.setdefault("log_directory", "logs")
    for key in ("gfs_base_url", "auth_token_url", "client_id", "username", "password", "output_directory", "log_directory", "output_name"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"Config requires a nonempty {key}.")
    for key in ("gfs_base_url", "auth_token_url"):
        url = urlsplit(config[key])
        if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError(f"Invalid {key}; use a URL without credentials or query parameters.")
    roots = config.get("root_paths")
    if not isinstance(roots, list) or not roots or any(
            not isinstance(root, str) or not root.startswith("/") or "//" in root or "\\" in root
            or any(part in (".", "..") for part in root.split("/")) for root in roots):
        raise ValueError("root_paths must contain absolute GFS folder paths.")
    config["root_paths"] = [root.rstrip("/") or "/" for root in roots]
    fields = config.get("fields")
    if not isinstance(fields, dict) or not fields or any(
            not isinstance(name, str) or not name or not isinstance(keys, list) or not keys
            or not all(isinstance(key, str) and key for key in keys) for name, keys in fields.items()):
        raise ValueError("fields must map CSV column names to lists of JSON keys.")
    required = config.get("required_fields", [])
    if not isinstance(required, list) or any(not isinstance(name, str) or name not in fields for name in required):
        raise ValueError("required_fields must contain configured CSV column names.")
    config["required_fields"] = required
    prefix = config.get("authorization_prefix", "")
    if not isinstance(prefix, str) or "\r" in prefix or "\n" in prefix:
        raise ValueError("Invalid authorization_prefix.")
    config["authorization_prefix"] = prefix
    for key, default in (("max_attempts", 3), ("timeout_seconds", 60)):
        config.setdefault(key, default)
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer.")
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export QA metadata from the GFS target in config.json.")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--limit", type=int, help="Export at most this many files for a small test run")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    try:
        config = load_config(args.config)
        output = args.config.resolve().parent / config["output_directory"]
        logs = args.config.resolve().parent / config["log_directory"]
        output.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S-%f")
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", config["output_name"]).strip(" .") or "qa_logs"
        csv_path, log_path = output / f"{name}_{stamp}.csv", logs / f"{name}_{stamp}.log"
        handler = logging.FileHandler(log_path, encoding="utf-8")
        logging.basicConfig(handlers=[handler], level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
        print(f"CSV: {csv_path}")
        print(f"Log: {log_path}")
        try:
            with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(config["fields"]))
                writer.writeheader()
                rows, errors, warnings, status = crawl(GfsClient(config, config["username"], config["password"]), config, writer, args.limit)
            print(f"{status}: {rows} files exported, {errors} errors, {warnings} warnings.")
            logging.info("%s: %s files, %s errors, %s warnings", status, rows, errors, warnings)
            return 2 if errors else 0
        except AuthError as error:
            logging.error("%s", error)
            print(f"Stopped: {error} CSV contains any rows already exported.", file=sys.stderr)
            return 3
        except KeyboardInterrupt:
            logging.warning("Interrupted; CSV contains rows already exported.")
            print("Interrupted. Rows already exported are saved.")
            return 130
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except (OSError, ValueError, EOFError) as error:
        # JSON decode messages omit the document contents and credentials.
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
