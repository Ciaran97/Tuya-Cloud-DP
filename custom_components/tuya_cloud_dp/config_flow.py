"""Config flow for Tuya Cloud DP Climate (UID-based, LocalTuya-style)."""
from __future__ import annotations

from typing import Any, Dict, Optional, List

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import CONF_REGION, CONF_DEVICE_ID
from homeassistant.helpers.selector import selector
import logging

from .const import DOMAIN
from .cloud_api import TuyaCloudApi

_LOGGER = logging.getLogger(__name__)

# Minimal inputs (match LocalTuya cloud style)
CONF_ACCESS_ID     = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_USER_ID       = "user_id"   # UID from "Link Tuya App Account" in Tuya IoT console

# DP mapping
CONF_SETPOINT_CODE = "setpoint_code"
CONF_CURTEMP_CODE  = "curtemp_code"
CONF_POWER_CODE    = "power_code"
CONF_MODE_CODE     = "mode_code"
CONF_MIN_TEMP      = "min_temp"
CONF_MAX_TEMP      = "max_temp"
CONF_PRECISION     = "precision"

REGIONS = ["us", "eu", "in", "cn"]

STEP1_SCHEMA = vol.Schema({
    vol.Required(CONF_REGION, default="us"): vol.In(REGIONS),
    vol.Required(CONF_ACCESS_ID): str,
    vol.Required(CONF_ACCESS_SECRET): str,
    vol.Required(CONF_USER_ID): str,  # UID (linked app account)
})

def _readable(x) -> str:
    if isinstance(x, dict):
        return x.get("msg") or x.get("message") or x.get("code") or str(x)
    return str(x)

def _parse_values_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v or {}

def _merge_functions(*payloads) -> Dict[str, Dict[str, Any]]:
    """Create code -> {type, values} from any number of Tuya 'functions' payloads."""
    out: Dict[str, Dict[str, Any]] = {}
    for p in payloads:
        if not p or not p.get("success"):
            continue
        res = p.get("result") or {}
        funcs = res.get("functions") or res.get("result") or []  # some responses nest differently
        for f in funcs:
            code = f.get("code")
            if not code:
                continue
            t = (f.get("type") or "").lower()
            vals = _parse_values_json(f.get("values"))
            out[code] = {"type": t, "values": vals}
    return out

def _extract_spec(spec: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Build a map of code -> {type, values} from BOTH functions and status."""
    out: Dict[str, Dict[str, Any]] = {}
    if not spec or not spec.get("success"):
        return out
    res = spec.get("result") or {}
    for f in (res.get("functions") or []):
        c = f.get("code")
        if c:
            out[c] = {"type": (f.get("type") or "").lower(), "values": f.get("values")}
    # Many devices only expose types in 'status' entries
    for s in (res.get("status") or []):
        c = s.get("code")
        if not c:
            continue
        typ = (s.get("type") or "").lower()
        if c not in out:
            out[c] = {"type": typ, "values": s.get("values")}
        else:
            # prefer a concrete type if we were missing/empty before
            if not out[c].get("type") and typ:
                out[c]["type"] = typ
    return out

def _extract_status_map(status_payload) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not status_payload or not status_payload.get("success"):
        return out
    for i in (status_payload.get("result") or []):
        c = i.get("code")
        if c:
            out[c] = i.get("value")
    return out

def _extract_status(status: Dict[str, Any]) -> Dict[str, Any]:
    """Map code -> current value."""
    out: Dict[str, Any] = {}
    if not status or not status.get("success"):
        return out
    for i in (status.get("result") or []):
        c = i.get("code")
        if c:
            out[c] = i.get("value")
    return out

def _label(code: str, dpid_index: Optional[int], value: Any) -> str:
    """Return a label like '1 – <code> (<value>)' for the dropdown."""
    # Fallback if no DPID index known
    dpid_str = str(dpid_index) if dpid_index is not None else "?"
    val_str = f"{value}" if value is not None else "N/A"
    return f"{dpid_str} – {code} ({val_str})"

def _label(code: str, typ: Optional[str], cur: Any) -> str:
    cur_s = "N/A" if cur is None else str(cur)
    t = typ or "?"
    return f"{code}  (type:{t}, current:{cur_s})"

def _dp_schema(spec_map: Dict[str, Dict[str, Any]], status_map: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> vol.Schema:
    defaults = defaults or {}

    # Build union of codes we know about
    all_codes = sorted(set(spec_map.keys()) | set(status_map.keys()))
    if not all_codes:
        # last resort: prevent empty form
        all_codes = ["temp_set", "temp_current", "Power", "Mode"]

    def typ_of(code: str) -> str:
        t = (spec_map.get(code, {}).get("type") or "").lower()
        if t:
            return t
        v = status_map.get(code)
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, (int, float)):
            return "integer"
        return ""

    # Partition by type
    numeric = [c for c in all_codes if typ_of(c) in ("integer", "float", "value")]
    boolean = [c for c in all_codes if typ_of(c) == "bool"]
    enum    = [c for c in all_codes if typ_of(c) == "enum"]

    # If we still don't have a numeric candidate for setpoint, allow any code
    numeric_fallback = numeric if numeric else all_codes

    def opt_list(codes):
        return [{"value": c, "label": _label(c, spec_map.get(c, {}).get("type"), status_map.get(c))} for c in codes]

    num_opts  = opt_list(numeric_fallback)
    bool_opts = opt_list(boolean)
    enum_opts = opt_list(enum)

    none_opt = {"value": "", "label": "(none)"}
    default_set = defaults.get("setpoint_code") or (numeric[0] if numeric else (all_codes[0] if all_codes else ""))

    sel = lambda opts: selector({"select": {"options": (opts or [{"value": "", "label": "(none)"}]), "mode": "dropdown"}})

    return vol.Schema({
        vol.Required("setpoint_code", default=default_set): sel(num_opts),
        vol.Optional("curtemp_code",  default=defaults.get("curtemp_code","")):  sel([none_opt] + num_opts),
        vol.Optional("power_code",    default=defaults.get("power_code","")):    sel([none_opt] + bool_opts),
        vol.Optional("mode_code",     default=defaults.get("mode_code","")):     sel([none_opt] + enum_opts),
        vol.Optional("min_temp", default=5):  vol.Coerce(float),
        vol.Optional("max_temp", default=35): vol.Coerce(float),
        vol.Optional("precision", default=1.0): vol.In([0.1, 0.5, 1.0]),
    })

def _error_schema(msg: str) -> vol.Schema:
    # add a read-only-ish field to display the actual error reason
    return STEP1_SCHEMA.extend({ vol.Optional("error_detail", default=str(msg)): str })

class TuyaCloudDPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._cfg1: Dict[str, Any] = {}
        self._devices: List[Dict[str, Any]] = []
        self._spec: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, Any] = {}

    async def async_step_user(self, user_input=None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP1_SCHEMA)

        try:
            self._cfg1 = dict(user_input)
            region = user_input[CONF_REGION]
            aid    = user_input[CONF_ACCESS_ID]
            sec    = user_input[CONF_ACCESS_SECRET]
            uid    = user_input[CONF_USER_ID].strip()

            # Try selected region, then the other of (us, eu) as a fallback
            region_order = [region] + ([r for r in ("us","eu") if r != region] if region in ("us","eu") else ["us","eu"])
            last_err = None
            api = None

            for rgn in region_order:
                try:
                    api = TuyaCloudApi(self.hass, rgn, aid, sec)
                    res = await api.grant_type_1()
                    if res != "ok":
                        last_err = res
                        api = None
                        continue

                    devs = await api.list_devices_for_uid(uid)
                    if not devs or not devs.get("success"):
                        last_err = devs
                        api = None
                        continue

                    # success
                    self._cfg1[CONF_REGION] = rgn
                    result = devs.get("result")
                    if isinstance(result, list):
                        self._devices = result
                    else:
                        self._devices = result or []
                    break
                except Exception as e:
                    api = None
                    last_err = str(e)

            if api is None:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_error_schema(_readable(last_err or "unknown")),
                    errors={"base": "cannot_connect"},
                )

            if not self._devices:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_error_schema("0 devices for this UID (check link/region)"),
                    errors={"base": "no_devices"},
                )

            return await self.async_step_pick_device()

        except Exception as e:
            _LOGGER.exception("Config step 'user' failed: %s", e)
            return self.async_show_form(
                step_id="user",
                data_schema=_error_schema(e),
                errors={"base": "unknown"},
            )

    async def async_step_pick_device(self, user_input=None) -> FlowResult:
        choices: Dict[str, str] = {}
        for d in self._devices:
            did = d.get("id") or d.get("device_id")
            if not did:
                continue
            name = d.get("name") or "Unnamed"
            prod = d.get("product_name") or d.get("category") or ""
            choices[f"{name} · {prod} · {did}"] = did

        if not choices:
            return self.async_abort(reason="no_devices")

        schema = vol.Schema({ vol.Required(CONF_DEVICE_ID): vol.In(choices) })
        if user_input is None:
            return self.async_show_form(step_id="pick_device", data_schema=schema)

        self._cfg1[CONF_DEVICE_ID] = user_input[CONF_DEVICE_ID]

        # Fetch spec/status to build DP dropdowns
                # Fetch functions/spec/status to build DP dropdowns (union, with live values)
        try:
            api = TuyaCloudApi(
                self.hass,
                self._cfg1[CONF_REGION],
                self._cfg1[CONF_ACCESS_ID],
                self._cfg1[CONF_ACCESS_SECRET],
            )
            if await api.grant_type_1() != "ok":
                return self.async_show_form(step_id="pick_device", data_schema=schema, errors={"base": "cannot_connect"})

            # Try both sources for functions; some tenants only fill one of them
            fx = await api.device_functions(self._cfg1[CONF_DEVICE_ID])
            spec = await api.device_spec(self._cfg1[CONF_DEVICE_ID])
            status = await api.device_status(self._cfg1[CONF_DEVICE_ID])

            funcs_map = _merge_functions(fx, spec)  # writable commands live here
            status_map = _extract_status_map(status)

            # If functions are totally empty, fall back to status-only so UI still shows codes.
            # Writes will still require real function codes, but at least the user can pick.
            self._spec = funcs_map
            self._status = status_map

            # Tiny debug so we can see counts in HA logs if needed
            _LOGGER.debug("DP build: functions=%d status=%d", len(self._spec), len(self._status))

        except Exception as e:
            _LOGGER.exception("pick_device fetch failed: %s", e)
            return self.async_show_form(step_id="pick_device", data_schema=schema, errors={"base": "unknown"})

    async def async_step_map_dp(self, user_input=None) -> FlowResult:
        schema = _dp_schema(self._spec, self._status)
        if user_input is None:
            return self.async_show_form(step_id="map_dp", data_schema=schema)

        data = {**self._cfg1, **user_input}
        await self.async_set_unique_id(data.get(CONF_DEVICE_ID))
        self._abort_if_unique_id_configured()
        title = f"Tuya Cloud DP ({data.get(CONF_DEVICE_ID)})"
        return self.async_create_entry(title=title, data=data)

    @callback
    def async_get_options_flow(self, entry: config_entries.ConfigEntry):
        return TuyaCloudDPOptionsFlow(entry)

class TuyaCloudDPOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry
        self._spec: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, Any] = {}

    async def async_step_init(self, user_input=None) -> FlowResult:
        from .cloud_api import TuyaCloudApi  # lazy import
        data = {**self.entry.data, **(self.entry.options or {})}
        api = TuyaCloudApi(self.hass, data[CONF_REGION], data[CONF_ACCESS_ID], data[CONF_ACCESS_SECRET])
        if await api.grant_type_1() == "ok":
            self._spec = _extract_spec(await api.device_spec(data[CONF_DEVICE_ID]))
            self._status = _extract_status(await api.device_status(data[CONF_DEVICE_ID]))

        defaults = {
            CONF_SETPOINT_CODE: data.get(CONF_SETPOINT_CODE, "temp_set"),
            CONF_CURTEMP_CODE:  data.get(CONF_CURTEMP_CODE, ""),
            CONF_POWER_CODE:    data.get(CONF_POWER_CODE, ""),
            CONF_MODE_CODE:     data.get(CONF_MODE_CODE, ""),
        }
        schema = _dp_schema(self._spec, self._status, defaults)

        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=schema)

        new_opts = {**(self.entry.options or {}), **user_input}
        return self.async_create_entry(title="", data=new_opts)