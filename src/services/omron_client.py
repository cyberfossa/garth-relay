"""Omron Connect API client implementation supporting both v1 and v2 regional APIs.

CREDENTIALS & LICENSE ATTRIBUTION:
This implementation contains logic and constants for interacting with Omron Connect APIs
(both v1 Kii Cloud and v2 direct APIs) inspired by the open-source project 'bugficks/omramin'
(licensed under the GNU General Public License v2.0 - GPL-2.0).

API v1 Kii-Cloud App IDs and Keys listed below are reverse-engineered from the official
Omron Connect mobile applications and are necessary for API interoperability:

- SG (Singapore / Asia Pacific):
    App ID: lou30y2xfa9f
    App Key: 392a4bdff8af4141944d30ca8e3cc860
- JP (Japan):
    App ID: 1e3ddd17
    App Key: b576cf704409ec86facdacc16fbaadad
- IN (India):
    App ID: cuoy728n
    App Key: 784uhaescyzyc28l2yh8xsth1xpga21g
- EU (Europe / EMEA):
    App ID: bfyy2kf1d5a0
    App Key: 989c6dbdc0244886ac2ba4de4892080e

License Warning:
The reference project 'bugficks/omramin' is licensed under GPL-2.0. Because this file
provides interoperability with the same proprietary API endpoints, it incorporates those
same protocol definitions and reverse-engineered keys. Developers using or distributing
this code should evaluate compatibility between the MIT license of garth-relay and the
GPL-2.0 license of the reference project.
"""

from __future__ import annotations

import datetime
import enum
import hashlib
import logging
import re
import zlib
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from typing import Any, get_type_hints

import httpx
import pytz
from httpx import HTTPStatusError

logger = logging.getLogger("omronconnect")


# Monkey-patch GZipDecoder to handle servers claiming gzip but sending uncompressed data
def _patched_gzip_decode(self: Any, data: bytes) -> bytes:
    try:
        return self.decompressor.decompress(data)
    except zlib.error as exc:
        try:
            data.decode("utf-8")
            logger.debug("GZip decompression failed but data is valid UTF-8: %s", exc)
            return data
        except UnicodeDecodeError:
            raise httpx.DecodingError(str(exc)) from exc


# Apply patch
try:
    # pylint: disable=protected-access
    if hasattr(httpx, "_decoders") and hasattr(httpx._decoders, "GZipDecoder"):
        setattr(httpx._decoders.GZipDecoder, "decode", _patched_gzip_decode)
except Exception:
    logger.exception("Failed to monkey patch httpx GZipDecoder")


class DeviceCategory(enum.StrEnum):
    BPM = "0"
    SCALE = "1"


class WeightUnit(enum.IntEnum):
    G = 8192
    KG = 8195
    LB = 8208
    ST = 8224


class ValueType(enum.StrEnum):
    EVENT_RECORD = "0"
    MMHG_MAX_FIGURE = "1"
    MMHG_MIN_FIGURE = "2"
    BPM_FIGURE = "3"
    ARRHYTHMIA_FLAG_FIGURE = "6"
    BODY_MOTION_FLAG_FIGURE = "7"
    KEEP_UP_CHECK_FIGURE = "8"
    KG_FIGURE = "257"
    BODY_FAT_PER_FIGURE = "259"
    BASAL_METABOLISM_FIGURE = "260"
    RATE_SKELETAL_MUSCLE_FIGURE = "261"
    BMI_FIGURE = "262"
    BIOLOGICAL_AGE_FIGURE = "263"
    VISCERAL_FAT_FIGURE = "264"


def _coerce_dataclass_fields(self: Any) -> None:
    type_hints = get_type_hints(type(self))
    for field in fields(self):
        attr = getattr(self, field.name)
        field_type = type_hints[field.name]
        if field_type == datetime.tzinfo:
            if not isinstance(attr, datetime.tzinfo):
                object.__setattr__(self, field.name, pytz.timezone(attr) if isinstance(attr, str) else attr)
        else:
            object.__setattr__(self, field.name, field_type(attr))


@dataclass(frozen=True)
class BodyIndexListItem:
    value: int
    subtype: int
    scale: int
    measurementId: int

    def __post_init__(self) -> None:
        _coerce_dataclass_fields(self)


@dataclass(frozen=True)
class BPMeasurement:
    systolic: int
    diastolic: int
    pulse: int
    measurementDate: int
    timeZone: datetime.tzinfo
    irregularHB: bool = False
    movementDetect: bool = False
    cuffWrapDetect: bool = True
    notes: str = ""

    def __post_init__(self) -> None:
        _coerce_dataclass_fields(self)


@dataclass(frozen=True)
class WeightMeasurement:
    weight: float
    measurementDate: int
    timeZone: datetime.tzinfo
    bmiValue: float = -1.0
    bodyFatPercentage: float = -1.0
    restingMetabolism: float = -1.0
    skeletalMusclePercentage: float = -1.0
    visceralFatLevel: float = -1.0
    metabolicAge: int = -1
    notes: str = ""

    def __post_init__(self) -> None:
        _coerce_dataclass_fields(self)


MeasurementTypes = BPMeasurement | WeightMeasurement


def ble_mac_to_serial(mac: str) -> str:
    values = mac.split(":")
    serial = "".join(values[5:2:-1] + ["fe", "ff"] + values[2::-1])
    return serial.lower()


def serial_to_mac(serial: str) -> str:
    values = [serial[i : i + 2] for i in range(0, len(serial), 2)]
    return ":".join(values[5:2:-1] + values[2::-1])


def convert_weight_to_kg(weight: float, unit: int) -> float:
    if unit == WeightUnit.G:
        return weight / 1000
    if unit == WeightUnit.LB:
        return weight * 0.45359237
    if unit == WeightUnit.ST:
        return weight * 6.35029318
    return weight


def convert_data_util(value: int, scale: int, _type: type[Any] = float) -> Any:
    if scale < 0:
        factor = Decimal("0.1") ** (-scale)
    else:
        factor = Decimal(10) ** scale
    return _type(Decimal(value) * factor)


@dataclass(frozen=True)
class OmronDevice:
    name: str
    macaddr: str
    category: DeviceCategory | None = None
    user: int = 1
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.category, DeviceCategory):
            try:
                if self.category:
                    object.__setattr__(self, "category", DeviceCategory(str(self.category)))
                else:
                    object.__setattr__(self, "enabled", False)
            except ValueError as exc:
                object.__setattr__(self, "enabled", False)
                raise ValueError(f"Device '{self.name}' has invalid category: '{self.category}'") from exc

    @property
    def serial(self) -> str:
        return ble_mac_to_serial(self.macaddr)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.category:
            result["category"] = self.category.name
        return result


def omron_model_to_device_category(model: str) -> DeviceCategory | None:
    if not model:
        return None
    rx = {
        re.compile(r"^(HEM-|X[0-9]+ Smart)", re.IGNORECASE): DeviceCategory.BPM,
        re.compile(r"^(HBF-|Body Composition Monitor)|(VIVA$)", re.IGNORECASE): DeviceCategory.SCALE,
    }
    for pattern, cat in rx.items():
        if pattern.search(model):
            return cat
    return None


# Server / Region mappings
def get_servers_for_region(region: str) -> list[str]:
    region = region.upper()
    if "US" in region:
        return ["https://vlt-mobile-api.prd.us.ohiomron.com/prd"]
    if "EMEA" in region or "EU" in region:
        return [
            "https://vlt-mobile-api.prd.eu.ohiomron.eu/prd",
            "https://oi-api.ohiomron.eu/app",
        ]
    if "AP" in region or "ASIA" in region:
        return ["https://data-sg.omronconnect.com/api"]
    return ["https://vlt-mobile-api.prd.eu.ohiomron.eu/prd"]


def get_default_country_for_region(region: str) -> str:
    region = region.upper()
    if "US" in region:
        return "US"
    if "EMEA" in region or "EU" in region:
        return "CZ"
    if "AP" in region or "ASIA" in region:
        return "SG"
    return "CZ"


def get_credentials_for_server(server_url: str) -> tuple[str, str] | None:
    if "data-sg" in server_url:
        return ("lou30y2xfa9f", "392a4bdff8af4141944d30ca8e3cc860")
    if "data-jp" in server_url:
        return ("1e3ddd17", "b576cf704409ec86facdacc16fbaadad")
    if "data-in" in server_url:
        return ("cuoy728n", "784uhaescyzyc28l2yh8xsth1xpga21g")
    if "data-eu" in server_url:
        return ("bfyy2kf1d5a0", "989c6dbdc0244886ac2ba4de4892080e")
    return None


def _http_add_checksum(request: httpx.Request) -> None:
    if request.method in ["POST", "DELETE"] and request.content:
        request.headers["Checksum"] = hashlib.sha256(request.content).hexdigest()


class OmronConnect(ABC):
    @abstractmethod
    def login(self, email: str, password: str, country: str) -> tuple[str, str, datetime.datetime] | None:
        pass

    @abstractmethod
    def refresh_oauth2(self, refresh_token: str, **kwargs: Any) -> tuple[str, str, datetime.datetime] | None:
        pass

    @abstractmethod
    def get_registered_devices(self, days: int | None = 30) -> list[OmronDevice] | None:
        pass

    @abstractmethod
    def get_measurements(
        self, device: OmronDevice, searchDateFrom: int = 0, searchDateTo: int = 0
    ) -> list[MeasurementTypes]:
        pass


class OmronConnect1(OmronConnect):
    _OGSC_APP_VERSION = "011.004.00000"
    _OGSC_SDK_VERSION = "000.005"
    _USER_AGENT = f"OmronConnect/{_OGSC_APP_VERSION}.001 CFNetwork/1335.0.3.4 Darwin/21.6.0)"

    def __init__(self, server: str, country: str):
        self._server = server
        self._country = country
        self._headers: dict[str, str] = {}

        credentials = get_credentials_for_server(server)
        if not credentials:
            raise ValueError(f"No API v1 credentials found for server: {server}")

        app_id, app_key = credentials
        self._APP_URL = f"/apps/{app_id}/server-code"

        self._client = httpx.Client(
            headers={
                "user-agent": OmronConnect1._USER_AGENT,
                "X-OGSC-SDK-Version": OmronConnect1._OGSC_SDK_VERSION,
                "X-OGSC-App-Version": OmronConnect1._OGSC_APP_VERSION,
                "X-Kii-AppID": app_id,
                "X-Kii-AppKey": app_key,
            },
        )

    def login(self, email: str, password: str, country: str) -> tuple[str, str, datetime.datetime] | None:
        authData = {"username": email, "password": password}
        r = self._client.post(f"{self._server}/oauth2/token", json=authData, headers=self._headers)
        r.raise_for_status()

        resp = r.json()
        try:
            access_token = resp["access_token"]
            refresh_token = resp["refresh_token"]
            expires_in = int(resp.get("expires_in", 3600))
            expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=expires_in)
            self._headers["authorization"] = f"Bearer {access_token}"
            return access_token, refresh_token, expires_at
        except KeyError:
            logger.error("login() failed: %s", r.text)
        return None

    def refresh_oauth2(self, refresh_token: str, **kwargs: Any) -> tuple[str, str, datetime.datetime] | None:
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        r = self._client.post(f"{self._server}/oauth2/token", json=data, headers=self._headers)
        r.raise_for_status()

        resp = r.json()
        try:
            access_token = resp["access_token"]
            new_refresh_token = resp["refresh_token"]
            expires_in = int(resp.get("expires_in", 3600))
            expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=expires_in)
            self._headers["authorization"] = f"Bearer {access_token}"
            return access_token, new_refresh_token, expires_at
        except KeyError:
            logger.error("refresh_oauth2() failed: %s", r.text)
        return None

    def get_registered_devices(self, days: int | None = 30) -> list[OmronDevice] | None:  # noqa: C901
        syncList = []
        lastSyncDate = (
            0
            if days is None
            else int((datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)).timestamp() * 1000)
        )
        payload = {"countOnlyFlag": 0, "lastSyncDate": lastSyncDate}

        while True:
            r = self._client.post(
                f"{self._server}{self._APP_URL}/versions/current/synchronizeDeviceConfData",
                headers=self._headers,
                json=payload,
            )
            r.raise_for_status()
            resp = r.json()

            returnedValue = resp.get("returnedValue", {})
            syncList.extend(returnedValue.get("syncList", []))

            nextPaginationKey = int(returnedValue.get("nextPaginationKey", 0))
            if not nextPaginationKey or lastSyncDate == nextPaginationKey:
                break

            payload["lastSyncDate"] = nextPaginationKey

        devices: dict[str, dict[str, Any]] = {}
        for sync in syncList:
            for cat in sync.get("deviceCategoryList", []):
                for model in cat.get("deviceModelList", []):
                    for dev in model.get("deviceSerialIDList", []):
                        if str(dev.get("userNumberInDevice")) == "0":
                            continue
                        key = f"{dev['deviceSerialID']}:{dev['userNumberInDevice']}"
                        devices.setdefault(
                            key,
                            {
                                "deviceCategory": cat.get("deviceCategory"),
                                "deviceModel": model["deviceModel"],
                                "deviceSerialID": dev["deviceSerialID"],
                                "userNumberInDevice": dev["userNumberInDevice"],
                            },
                        )

        result: list[OmronDevice] = []
        for device in devices.values():
            category = None
            deviceCategory = device.get("deviceCategory")
            if not deviceCategory:
                deviceCategory = omron_model_to_device_category(device.get("deviceModel", ""))

            if deviceCategory is not None and deviceCategory != "":
                try:
                    category = DeviceCategory(str(deviceCategory))
                except ValueError:
                    continue

            ocDev = OmronDevice(
                category=category,
                name=f"{device['deviceModel']}:{device['userNumberInDevice']}",
                macaddr=serial_to_mac(device["deviceSerialID"]),
                user=int(device["userNumberInDevice"]),
            )
            result.append(ocDev)

        return result

    def get_measurements(
        self, device: OmronDevice, searchDateFrom: int = 0, searchDateTo: int = 0
    ) -> list[MeasurementTypes]:
        data = {
            "containCorrectedDataFlag": 1,
            "containAllDataTypeFlag": 1,
            "deviceCategory": device.category,
            "deviceSerialID": device.serial,
            "userNumberInDevice": int(device.user),
            "searchDateFrom": searchDateFrom if searchDateFrom >= 0 else 0,
            "searchDateTo": (
                int(datetime.datetime.now(datetime.UTC).timestamp() * 1000) if searchDateTo <= 0 else searchDateTo
            ),
        }

        r = self._client.post(
            f"{self._server}{self._APP_URL}/versions/current/measureData", json=data, headers=self._headers
        )
        r.raise_for_status()

        resp = r.json()
        returnedValue = resp.get("returnedValue")
        if not returnedValue:
            return []

        if isinstance(returnedValue, list):
            returnedValue = returnedValue[0]

        if isinstance(returnedValue, dict) and "errorCode" in returnedValue:
            return []

        measurements: list[MeasurementTypes] = []
        devCat = DeviceCategory(returnedValue["deviceCategory"])
        deviceModelList = returnedValue["deviceModelList"]
        if not deviceModelList:
            return measurements

        for devModel in deviceModelList:
            deviceSerialIDList = devModel.get("deviceSerialIDList", [])
            for dev in deviceSerialIDList:
                if dev.get("deviceSerialID") != device.serial:
                    continue
                if devCat == DeviceCategory.BPM:
                    measurements.extend(self._process_bpm_measurements(dev))
                elif devCat == DeviceCategory.SCALE:
                    measurements.extend(self._process_scale_measurements(dev))
                break

        return measurements

    def _process_bpm_measurements(self, dev: dict[str, Any]) -> list[BPMeasurement]:
        measurements: list[BPMeasurement] = []
        for m in dev.get("measureList", []):
            bodyIndexList = {k: BodyIndexListItem(*v) for k, v in m["bodyIndexList"].items()}
            systolic = convert_data_util(
                bodyIndexList[ValueType.MMHG_MAX_FIGURE].value,
                bodyIndexList[ValueType.MMHG_MAX_FIGURE].scale,
                int,
            )
            diastolic = convert_data_util(
                bodyIndexList[ValueType.MMHG_MIN_FIGURE].value,
                bodyIndexList[ValueType.MMHG_MIN_FIGURE].scale,
                int,
            )
            pulse = convert_data_util(
                bodyIndexList[ValueType.BPM_FIGURE].value,
                bodyIndexList[ValueType.BPM_FIGURE].scale,
                int,
            )
            bodymotion = bodyIndexList[ValueType.BODY_MOTION_FLAG_FIGURE].value
            irregHB = bodyIndexList[ValueType.ARRHYTHMIA_FLAG_FIGURE].value
            cuffWrapGuid = bodyIndexList[ValueType.KEEP_UP_CHECK_FIGURE].value
            timeZone = pytz.timezone(m["timeZone"])

            bp = BPMeasurement(
                systolic=systolic,
                diastolic=diastolic,
                pulse=pulse,
                measurementDate=m["measureDateTo"],
                timeZone=timeZone,
                irregularHB=irregHB != 0,
                movementDetect=bodymotion != 0,
                cuffWrapDetect=cuffWrapGuid != 0,
            )
            measurements.append(bp)
        return measurements

    def _process_scale_measurements(self, dev: dict[str, Any]) -> list[WeightMeasurement]:
        measurements: list[WeightMeasurement] = []
        for m in dev.get("measureList", []):
            bodyIndexList = {k: BodyIndexListItem(*v) for k, v in m["bodyIndexList"].items()}
            weight_entry = bodyIndexList[ValueType.KG_FIGURE]
            weight = convert_data_util(weight_entry.value, weight_entry.scale)
            weight = convert_weight_to_kg(weight, weight_entry.subtype)
            bodyFat = convert_data_util(
                bodyIndexList[ValueType.BODY_FAT_PER_FIGURE].value,
                bodyIndexList[ValueType.BODY_FAT_PER_FIGURE].scale,
            )
            skeletalMuscle = convert_data_util(
                bodyIndexList[ValueType.RATE_SKELETAL_MUSCLE_FIGURE].value,
                bodyIndexList[ValueType.RATE_SKELETAL_MUSCLE_FIGURE].scale,
            )
            basalMet = convert_data_util(
                bodyIndexList[ValueType.BASAL_METABOLISM_FIGURE].value,
                bodyIndexList[ValueType.BASAL_METABOLISM_FIGURE].scale,
            )
            metAge = convert_data_util(
                bodyIndexList[ValueType.BIOLOGICAL_AGE_FIGURE].value,
                bodyIndexList[ValueType.BIOLOGICAL_AGE_FIGURE].scale,
                int,
            )
            viscLevel = convert_data_util(
                bodyIndexList[ValueType.VISCERAL_FAT_FIGURE].value,
                bodyIndexList[ValueType.VISCERAL_FAT_FIGURE].scale,
            )
            bmi = convert_data_util(
                bodyIndexList[ValueType.BMI_FIGURE].value,
                bodyIndexList[ValueType.BMI_FIGURE].scale,
            )
            timeZone = pytz.timezone(m["timeZone"])

            wm = WeightMeasurement(
                weight=weight,
                measurementDate=m["measureDateTo"],
                timeZone=timeZone,
                bmiValue=bmi,
                bodyFatPercentage=bodyFat,
                restingMetabolism=basalMet,
                skeletalMusclePercentage=skeletalMuscle,
                visceralFatLevel=viscLevel,
                metabolicAge=metAge,
            )
            measurements.append(wm)
        return measurements


class OmronConnect2(OmronConnect):
    _APP_NAME = "OCM"
    _APP_VERSION = "8.2.1"
    _USER_AGENT = (
        f"OMRON connect/{_APP_VERSION} (com.omronhealthcare.omronconnect; build:24; iOS 18.7.2) Alamofire/5.9.1"
    )

    def __init__(self, server: str, country: str):
        self._server = server
        self._country = country
        self._headers: dict[str, str] = {}
        self._email: str = ""

        self._client = httpx.Client(
            event_hooks={"request": [_http_add_checksum]},
            headers={"user-agent": OmronConnect2._USER_AGENT},
        )
        self._v2 = "/v2" if "/app" in server else ""

    def login(self, email: str, password: str, country: str) -> tuple[str, str, datetime.datetime] | None:
        data = {
            "emailAddress": email,
            "password": password,
            "country": country,
            "app": self._APP_NAME,
        }
        r = self._client.post(f"{self._server}/login", json=data)
        r.raise_for_status()

        resp = r.json()
        try:
            accessToken = resp["accessToken"]
            refreshToken = resp["refreshToken"]
            expires_in = int(resp.get("expiresIn", 3600))
            expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=expires_in)
            self._headers["authorization"] = f"{accessToken}"
            self._email = email
            self._country = country
            return accessToken, refreshToken, expires_at
        except KeyError:
            logger.error("login() v2 failed: %s", r.text)
        return None

    def refresh_oauth2(self, refresh_token: str, **kwargs: Any) -> tuple[str, str, datetime.datetime] | None:
        data = {
            "app": self._APP_NAME,
            "emailAddress": kwargs.get("email", self._email),
            "refreshToken": refresh_token,
        }
        r = self._client.post(f"{self._server}/login", json=data, headers=self._headers)
        r.raise_for_status()

        resp = r.json()
        try:
            accessToken = resp["accessToken"]
            new_refreshToken = resp["refreshToken"]
            expires_in = int(resp.get("expiresIn", 3600))
            expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=expires_in)
            self._headers["authorization"] = f"{accessToken}"
            return accessToken, new_refreshToken, expires_at
        except KeyError:
            logger.error("refresh_oauth2() v2 failed: %s", r.text)
        return None

    def get_registered_devices(self, days: int | None = 30) -> list[OmronDevice] | None:
        r = self._client.get(f"{self._server}{self._v2}/init-user?app={self._APP_NAME}", headers=self._headers)
        r.raise_for_status()
        resp = r.json()
        device_list = resp.get("data", {}).get("deviceList", [])

        result: list[OmronDevice] = []
        for device in device_list:
            attrs = device.get("attributes", {})
            if not attrs.get("isActive", 0):
                continue
            macAddress = attrs.get("macAddress", "").strip()
            if not macAddress:
                continue

            category = None
            deviceCategory = attrs.get("deviceCategory")
            if not deviceCategory:
                deviceModel = attrs.get("deviceModel", attrs.get("identifier", ""))
                deviceCategory = omron_model_to_device_category(deviceModel)

            if deviceCategory is not None and deviceCategory != "":
                try:
                    category = DeviceCategory(str(deviceCategory))
                except ValueError:
                    continue

            deviceModel = attrs.get("deviceModel", attrs.get("identifier", "Unknown"))
            userNumberInDevice = int(attrs.get("userNumberInDevice", 1))

            ocDev = OmronDevice(
                category=category,
                name=f"{deviceModel}:{userNumberInDevice}",
                macaddr=macAddress,
                user=userNumberInDevice,
            )
            result.append(ocDev)
        return result

    def get_bp_measurements(
        self, nextpaginationKey: int = 0, lastSyncedTime: int = 0, phoneIdentifier: str = ""
    ) -> list[dict[str, Any]]:
        _lastSyncedTime = "" if lastSyncedTime <= 0 else lastSyncedTime
        r = self._client.get(
            f"{self._server}{self._v2}/sync/bp?nextpaginationKey={nextpaginationKey}"
            f"&lastSyncedTime={_lastSyncedTime}&phoneIdentifier={phoneIdentifier}",
            headers=self._headers,
        )
        r.raise_for_status()
        return r.json()["data"]

    def get_weighins(
        self, nextpaginationKey: int = 0, lastSyncedTime: int = 0, phoneIdentifier: str = ""
    ) -> list[dict[str, Any]]:
        _lastSyncedTime = "" if lastSyncedTime <= 0 else lastSyncedTime
        r = self._client.get(
            f"{self._server}{self._v2}/sync/weight?nextpaginationKey={nextpaginationKey}"
            f"&lastSyncedTime={_lastSyncedTime}&phoneIdentifier={phoneIdentifier}",
            headers=self._headers,
        )
        r.raise_for_status()
        return r.json()["data"]

    def get_measurements(  # noqa: C901
        self, device: OmronDevice, searchDateFrom: int = 0, searchDateTo: int = 0
    ) -> list[MeasurementTypes]:
        user = int(device.user)

        def filter_measurements(data: list[dict[str, Any]]) -> list[MeasurementTypes]:
            r: list[MeasurementTypes] = []
            for m in data:
                if user >= 0 and int(m["userNumberInDevice"]) != user:
                    continue
                measurementDate = int(m["measurementDate"])
                if 0 < searchDateTo < measurementDate:
                    continue
                if int(m["isManualEntry"]):
                    continue

                if device.category == DeviceCategory.BPM:
                    bpm = BPMeasurement(
                        systolic=m["systolic"],
                        diastolic=m["diastolic"],
                        pulse=m["pulse"],
                        measurementDate=measurementDate,
                        timeZone=pytz.FixedOffset(int(m["timeZone"]) // 60),
                        irregularHB=int(m["irregularHB"]) != 0,
                        movementDetect=int(m["movementDetect"]) != 0,
                        cuffWrapDetect=int(m["cuffWrapDetect"]) != 0,
                        notes=m.get("notes", ""),
                    )
                    r.append(bpm)
                elif device.category == DeviceCategory.SCALE:
                    weight = float(m["weight"])
                    weightInLbs = float(m["weightInLbs"])
                    if weight <= 0 < weightInLbs:
                        weight = weightInLbs * 0.453592
                    wm = WeightMeasurement(
                        weight=weight,
                        measurementDate=measurementDate,
                        timeZone=pytz.FixedOffset(int(m["timeZone"]) // 60),
                        bmiValue=m["bmiValue"],
                        bodyFatPercentage=m["bodyFatPercentage"],
                        restingMetabolism=m["restingMetabolism"],
                        skeletalMusclePercentage=m["skeletalMusclePercentage"],
                        visceralFatLevel=m["visceralFatLevel"],
                        notes=m.get("notes", ""),
                    )
                    r.append(wm)
            return r

        data = None
        if device.category == DeviceCategory.BPM:
            data = self.get_bp_measurements(lastSyncedTime=searchDateFrom)
        elif device.category == DeviceCategory.SCALE:
            data = self.get_weighins(lastSyncedTime=searchDateFrom)

        return filter_measurements(data) if data else []


def get_omron_connect(server: str, country: str) -> OmronConnect:
    if re.search(r"data-([a-z]{2})\.omronconnect\.com", server):
        return OmronConnect1(server, country)
    return OmronConnect2(server, country)


def try_servers(servers: list[str], country: str, operation: Any) -> tuple[OmronConnect, Any]:
    for server in servers:
        try:
            oc = get_omron_connect(server, country)
            result = operation(oc)
            return oc, result
        except (httpx.ConnectError, httpx.TimeoutException, HTTPStatusError):
            continue
    raise ConnectionError("All servers failed")


class OmronClient:
    """Facade for managing server fallback and connection to Omron APIs."""

    def __init__(self, region: str):
        self.region = region
        self.servers = get_servers_for_region(region)
        self.country = get_default_country_for_region(region)
        self._active_client: OmronConnect | None = None

    def login(self, email: str, password: str) -> tuple[str, str, datetime.datetime] | None:
        def login_op(oc_instance: OmronConnect) -> tuple[str, str, datetime.datetime] | None:
            return oc_instance.login(email, password, self.country)

        self._active_client, tokens = try_servers(self.servers, self.country, login_op)
        return tokens

    def refresh_oauth2(self, refresh_token: str, **kwargs: Any) -> tuple[str, str, datetime.datetime] | None:
        def refresh_op(oc_instance: OmronConnect) -> tuple[str, str, datetime.datetime] | None:
            return oc_instance.refresh_oauth2(refresh_token, **kwargs)

        self._active_client, tokens = try_servers(self.servers, self.country, refresh_op)
        return tokens

    def get_registered_devices(self) -> list[OmronDevice] | None:
        if not self._active_client:
            raise RuntimeError("Not connected - call login() or refresh_oauth2() first")
        return self._active_client.get_registered_devices(days=None)

    def get_measurements(
        self, device: OmronDevice, searchDateFrom: int = 0, searchDateTo: int = 0
    ) -> list[MeasurementTypes]:
        if not self._active_client:
            raise RuntimeError("Not connected - call login() or refresh_oauth2() first")
        return self._active_client.get_measurements(device, searchDateFrom, searchDateTo)
