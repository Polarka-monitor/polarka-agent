"""The bazaar extension of x402: how a seller says what it sells, and a facilitator's catalog of it.

A seller puts `extensions.bazaar` into its 402 (what the resource takes and gives, with a JSON schema of that),
and a buyer echoes it into its payment. The facilitator, once a payment is settled, writes the resource into its
catalog, and answers GET /discovery/resources and GET /discovery/search with it, so that an agent can find what
is for sale before it pays anything. specs/extensions/bazaar.md of the x402 repository is the source of the rules
below; the checks of serviceName, tags, iconUrl and routeTemplate follow it.

What this catalog takes is only what its owner lets in: resources whose origin is in `origins`. A facilitator that
settles for others' sellers decides that itself; ours catalogs only what is ours."""
import datetime
import ipaddress
import json
import os
import re
import socket
import threading
import urllib.parse

VERSION = 2
MOST_ITEMS = 1000           # resources kept
MOST_BYTES = 8192           # one extension, as JSON
METHODS_QUERY = ("GET", "HEAD", "DELETE")
METHODS_BODY = ("POST", "PUT", "PATCH")
PRINTABLE = re.compile(r"^[\x20-\x7e]+$")
TEMPLATE = re.compile(r"^/[a-zA-Z0-9_/:.\-~%]+$")
LOOPBACK = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
SCHEMAS = {"https://json-schema.org/draft/2020-12/schema", "http://json-schema.org/draft-07/schema#", "https://json-schema.org/draft-07/schema#"}


def service_name(value):
    return value if isinstance(value, str) and 0 < len(value) <= 32 and PRINTABLE.match(value) else None


def tags(value):
    if not isinstance(value, list):
        return None
    kept, seen = [], set()
    for t in value:
        if isinstance(t, str) and 0 < len(t) <= 32 and PRINTABLE.match(t) and t.lower() not in seen:
            kept.append(t)
            seen.add(t.lower())
        if len(kept) == 5:
            break
    return kept or None


def icon_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        u = urllib.parse.urlsplit(value)
        host = urllib.parse.unquote(u.hostname or "").encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError):
        return None
    if u.scheme not in ("http", "https") or not host or "@" in u.netloc or host in LOOPBACK:
        return None
    if host.isdigit() or re.match(r"^0x[0-9a-f]+$", host) or host.endswith(".") or host.endswith(".localhost"):
        return None
    try:
        socket.inet_aton(host)          # 127.1, 0177.0.0.1, 0x7f.0.0.1: what a browser reads as an address
        return None
    except OSError:
        pass
    try:
        ipaddress.ip_address(host.strip("[]"))
        return None
    except ValueError:
        return value


def route_template(value):
    if not isinstance(value, str) or not value or not TEMPLATE.match(value):
        return None
    plain = urllib.parse.unquote(value)
    return None if ".." in plain or "://" in plain or plain.startswith("//") else value


def _refs_inside(obj):
    """$ref and $id only as fragments of the same document."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("$ref", "$id", "$dynamicRef", "$recursiveRef", "$anchor", "$dynamicAnchor") and not (isinstance(v, str) and v.startswith("#")):
                return False
            if k == "$schema" and not (isinstance(v, str) and v in SCHEMAS):
                return False
            if not _refs_inside(v):
                return False
    elif isinstance(obj, list):
        return all(_refs_inside(v) for v in obj)
    return True


def check(extension):
    """(the extension as it is kept, None) or (None, why it is not taken)."""
    if not isinstance(extension, dict):
        return None, "the bazaar extension is not an object"
    if len(json.dumps(extension)) > MOST_BYTES:
        return None, "the bazaar extension is larger than %d bytes" % MOST_BYTES
    info, schema = extension.get("info"), extension.get("schema")
    if not isinstance(info, dict) or not isinstance(info.get("input"), dict) or not isinstance(schema, dict):
        return None, "info.input and schema are required"
    inp = info["input"]
    if inp.get("type") == "http":
        method = inp.get("method")
        if method in METHODS_BODY:
            if inp.get("bodyType") not in ("json", "form-data", "text"):
                return None, "a body method needs bodyType json, form-data or text"
        elif method not in METHODS_QUERY:
            return None, "info.input.method is not one of %s" % ", ".join(METHODS_QUERY + METHODS_BODY)
    elif inp.get("type") == "mcp":
        if not isinstance(inp.get("toolName"), str) or not isinstance(inp.get("inputSchema"), dict):
            return None, "an mcp input needs toolName and inputSchema"
    else:
        return None, "info.input.type is http or mcp"
    out = info.get("output")
    if out is not None and not (isinstance(out, dict) and isinstance(out.get("type"), str)):
        return None, "info.output needs a type"
    if not _refs_inside(schema) or not _refs_inside(info):
        return None, "$ref and $id must point inside the same document"
    kept = {"info": info, "schema": schema}
    template = route_template(extension.get("routeTemplate"))
    if template:
        kept["routeTemplate"] = template
    return kept, None


def declaration(method="GET", path_params=None, query_params=None, output_example=None, route=None, output_type="json"):
    """The bazaar extension a seller puts into its 402 for one HTTP route that takes no body. What the resource is
    goes into the 402's resource.description, not here."""
    inp = {"type": "http", "method": method}
    props = {"type": {"type": "string", "const": "http"}, "method": {"type": "string", "enum": [method]}}
    if path_params:
        inp["pathParams"] = dict(path_params)
        props["pathParams"] = {"type": "object", "properties": {k: {"type": "string"} for k in path_params},
                               "required": sorted(path_params)}
    if query_params:
        inp["queryParams"] = dict(query_params)
        props["queryParams"] = {"type": "object", "properties": {k: {"type": "string"} for k in query_params}}
    info = {"input": inp}
    out_schema = {"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]}
    if output_example is not None:
        info["output"] = {"type": output_type, "example": output_example}
        out_schema["properties"]["example"] = {"type": "object"}
    ext = {"info": info,
           "schema": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                      "properties": {"input": {"type": "object", "properties": props, "required": ["type", "method"],
                                               "additionalProperties": False},
                                     "output": out_schema},
                      "required": ["input"]}}
    if route:
        ext["routeTemplate"] = route
    return ext


def origin(url):
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    return "%s://%s" % (u.scheme, u.netloc.lower())


class Catalog:
    """What the facilitator settled and the seller let be found, kept in one JSON file."""

    def __init__(self, path, origins, clock=None):
        import time
        self.path, self.clock = path, clock or time.time
        self.origins = {o for o in (origin(x) for x in origins) if o}
        self.lock = threading.Lock()
        try:
            with open(path) as f:
                self.items = json.load(f).get("items", {})
        except (FileNotFoundError, ValueError):
            self.items = {}

    def _save(self):
        tmp = self.path + ".new"
        with open(tmp, "w") as f:
            json.dump({"items": self.items}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def add(self, payment, wanted):
        """After a settled payment: {"status": ...} for the EXTENSION-RESPONSES of this answer, or None when the
        payment carries no bazaar extension."""
        ext = (payment.get("extensions") or {}).get("bazaar") if isinstance(payment.get("extensions"), dict) else None
        if ext is None:
            return None
        kept, why = check(ext)
        resource = payment.get("resource") if isinstance(payment.get("resource"), dict) else {}
        url = resource.get("url")
        if kept is None:
            return {"status": "rejected", "rejectedReason": why}
        if not isinstance(url, str) or origin(url) is None:
            return {"status": "rejected", "rejectedReason": "resource.url must be an absolute http(s) URL"}
        if origin(url) not in self.origins:
            return {"status": "rejected", "rejectedReason": "this facilitator catalogs only resources of its owner"}
        u = urllib.parse.urlsplit(url)
        key_path = kept.get("routeTemplate") or u.path
        method = kept["info"]["input"].get("method") or kept["info"]["input"].get("toolName")
        key = "%s %s%s" % (method, origin(url), key_path)
        item = {"resource": origin(url) + key_path if kept.get("routeTemplate") else url, "type": kept["info"]["input"]["type"],
                "x402Version": VERSION, "accepts": [{k: wanted[k] for k in wanted if k != "resource"}],
                "lastUpdated": datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "extensions": {"bazaar": kept}}
        for name in ("description", "mimeType"):
            if isinstance(resource.get(name), str) and len(resource[name]) <= 500:
                item[name] = resource[name]
        for name, rule in (("serviceName", service_name), ("tags", tags), ("iconUrl", icon_url)):
            v = rule(resource.get(name))
            if v is not None:
                item[name] = v
        with self.lock:
            if key not in self.items and len(self.items) >= MOST_ITEMS:
                return {"status": "rejected", "rejectedReason": "the catalog is full"}
            self.items[key] = item
            self._save()
        return {"status": "success"}

    def _chosen(self, params):
        with self.lock:
            items = sorted(self.items.values(), key=lambda i: i["resource"])
        def fits(i):
            if params.get("type") and i["type"] != params["type"]:
                return False
            if params.get("extensions") and params["extensions"] not in (i.get("extensions") or {}):
                return False
            a = i["accepts"][0] if i["accepts"] else {}
            for k in ("payTo", "scheme", "network"):
                if params.get(k) and a.get(k) != params[k]:
                    return False
            return True
        return [i for i in items if fits(i)]

    @staticmethod
    def _number(value, default, most):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(0, min(n, most))

    def resources(self, params):
        items = self._chosen(params)
        limit = self._number(params.get("limit"), 100, 100)
        offset = self._number(params.get("offset"), 0, 10 ** 6)
        return {"x402Version": VERSION, "items": items[offset:offset + limit],
                "pagination": {"limit": limit, "offset": offset, "total": len(items)}}

    def search(self, params):
        words = []
        for w in re.split(r"\W+", str(params.get("query") or "")[:200].lower()):
            if w and w not in words:
                words.append(w)
        words = words[:8]
        items = self._chosen(params)
        def text(i):
            return " ".join([i["resource"], i.get("description", ""), i.get("serviceName", ""), " ".join(i.get("tags", [])),
                             str(i["extensions"]["bazaar"]["info"]["input"].get("description", ""))]).lower()
        hits = [i for i in items if (lambda t: all(w in t for w in words))(text(i))] if words else items
        limit = self._number(params.get("limit"), 20, 100)
        return {"x402Version": VERSION, "resources": hits[:limit], "partialResults": len(hits) > limit, "pagination": None}
