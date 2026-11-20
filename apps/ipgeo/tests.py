"""IP 归属地 (ipgeo app): looking addresses up, the names of the stored locations, storing them with the IPs
(logins, the activity log, the download log), the /manage/#ip-geo API, downloading the database and 补全.

Nothing goes to the network: the databases are small real MaxMind DB files (apps/ipgeo/testing.py), and
downloads come from a fake jobs.fetch(). Jobs run in place (jobs.run), except in ThreadTests.
"""

import hashlib
import io
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from unittest import mock

import maxminddb
from django.db import connection, transaction
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone, translation
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.activity.models import ActivityLog
from apps.core import net
from apps.core.testing import admin_url
from apps.links.models import DownloadLog
from apps.links.tests.helpers import LinksTestCase

from . import jobs, lookup, places
from .lookup import Locator, open_reader, place_for
from .models import IpGeoSettings, PlaceName
from .sources import FILE_NAME, LOCATION_MAX, UNKNOWN, URL
from .testing import CHINA, ENGLAND, HANGZHOU, IPSWICH, NETWORKS, UNITED_KINGDOM, ZHEJIANG, build_mmdb, city, place, sample_database

API = "/api/v1/manage/ip-geo/"
SESSION = "/api/v1/session/"

# The locations of a few of testing.NETWORKS.
IN_HANGZHOU = "CN 1784764 1808926"  # 223.5.5.5, 2400:3200::1
IN_BEIJING = "CN 2038349 1816670"  # 36.110.1.1
IN_TAIPEI = "TW 7280290 1668341"  # 1.160.0.1
IN_HONG_KONG = "HK 1819729"  # 202.67.240.1
IN_IPSWICH = "GB 6269131 2646057"  # 81.2.69.142
IN_MOSCOW = "RU 524901"  # 77.88.1.1
IN_MOUNTAIN_VIEW = "US 5332921 5375480"  # 2001:4860:4860::8888


class FakeResponse(io.BytesIO):
    def __init__(self, body, length=True):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))} if length else {}


class FakeServer:
    """Stands in for jobs.fetch(): answers from {url: bytes or an exception}; anything else is a 404."""

    def __init__(self, files, length=True):
        self.files = files
        self.length = length
        self.asked = []

    def __call__(self, request):
        url = request.full_url
        self.asked.append(url)
        answer = self.files.get(url)
        if answer is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if isinstance(answer, BaseException):
            raise answer
        return FakeResponse(answer, self.length)


class GeoTestMixin:
    """A temporary [paths] geoip folder; install() puts a database in, as a download would, and turns IP 归属地 on."""

    def setUp(self):
        super().setUp()
        self.folder = Path(tempfile.mkdtemp(prefix="hfile-test-geoip-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        folder = override_settings(GEOIP_DIR=self.folder)
        folder.enable()
        self.addCleanup(folder.disable)
        lookup.forget_cached()
        self.addCleanup(lookup.forget_cached)
        lookup.forget_remembered()  # what another test's committed transaction saved isn't in this one's database
        self.addCleanup(lookup.forget_remembered)

    def install(self, data=None, enabled=True):
        data = sample_database() if data is None else data
        (self.folder / FILE_NAME).write_bytes(data)
        IpGeoSettings.objects.update_or_create(
            pk=1,
            defaults={
                "enabled": enabled,
                "file_name": FILE_NAME,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "downloaded_at": timezone.now(),
            },
        )

    def turn(self, enabled):
        IpGeoSettings.objects.update_or_create(pk=1, defaults={"enabled": enabled})

    def run_jobs_in_place(self):
        patcher = mock.patch.object(jobs, "start_thread", lambda work, job_id, *args: jobs.run(work, job_id, *args))
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, files, length=True):
        server = FakeServer(files, length)
        patcher = mock.patch.object(jobs, "fetch", server)
        patcher.start()
        self.addCleanup(patcher.stop)
        return server


def admin_client(**credentials):
    admin = User.objects.create_user("boss", password="pw-boss-2026", is_staff=True, **credentials)
    client = APIClient()
    client.force_authenticate(admin)
    return admin, client


# ------------------------------------------------------------------ locations and their names


class PlacesTests(SimpleTestCase):
    NAMES = {
        "CN": {"en": "China", "zh-CN": "中国"},
        "1784764": {"en": "Zhejiang", "zh-CN": "浙江"},
        "1808926": {"en": "Hangzhou", "zh-CN": "杭州"},
        "2038349": {"en": "Beijing", "zh-CN": "北京市"},
        "1816670": {"en": "Beijing", "zh-CN": "北京"},
        "US": {"en": "United States", "zh-CN": "美国"},
        "RU": {"en": "Russia"},
        "524901": {"en": "Moscow"},
        "HK": {"en": "Hong Kong", "zh-CN": "香港"},
        "1819729": {"en": "Hong Kong", "zh-CN": "香港"},
        "TW": {"en": "Taiwan", "zh-CN": "台湾"},
        "7280290": {"en": "Taipei City", "zh-CN": "台北市"},
        "1668341": {"en": "Taipei", "zh-CN": "台北市"},
        "SG": {"en": "Singapore", "zh-CN": "新加坡"},
        "1880252": {"en": "Singapore", "zh-CN": "新加坡"},
    }

    def names(self, *locations):
        return [places.name(location, self.NAMES) for location in locations]

    def test_from_the_largest_place_in_chinese_from_the_smallest_in_english(self):
        cases = [IN_HANGZHOU, "CN 1784764", "CN 1808926", "CN"]
        self.assertEqual(self.names(*cases), ["中国 浙江 杭州", "中国 浙江", "中国 杭州", "中国"])
        with translation.override("en"):
            self.assertEqual(self.names(*cases), ["Hangzhou, Zhejiang, China", "Zhejiang, China", "Hangzhou, China", "China"])

    def test_names_the_language_lacks(self):
        self.assertEqual(self.names(IN_MOSCOW), ["Russia Moscow"])  # no Chinese names: the English ones
        with translation.override("en"):
            self.assertEqual(self.names(IN_MOSCOW), ["Moscow, Russia"])
        # A place without any name is left out, a country is shown as its code.
        self.assertEqual(self.names("CN 123 1808926", "CN 123", "ZZ", "ZZ 1808926"), ["中国 杭州", "中国", "ZZ", "ZZ 杭州"])

    def test_one_place_under_two_names(self):
        cases = [IN_BEIJING, IN_TAIPEI, IN_HONG_KONG, "SG 1880252"]
        self.assertEqual(self.names(*cases), ["中国 北京", "中国台湾 台北市", "中国香港", "新加坡"])
        with translation.override("en"):
            self.assertEqual(self.names(*cases), ["Beijing, China", "Taipei, Taiwan", "Hong Kong", "Singapore"])

    def test_hong_kong_macao_and_taiwan(self):
        self.assertEqual(self.names("HK", "MO", "TW", "TW 7280290"), ["中国香港", "中国澳门", "中国台湾", "中国台湾 台北市"])
        with translation.override("en"):
            self.assertEqual(self.names("HK", "MO", "TW"), ["Hong Kong", "Macao", "Taiwan"])

    def test_what_isnt_a_place(self):
        self.assertEqual(self.names("PRIVATE", UNKNOWN), ["内网", "未知"])
        with translation.override("en"):
            self.assertEqual(self.names("PRIVATE", UNKNOWN), ["Private network", "Unknown"])

    def test_values_of_an_unexpected_shape(self):
        for names in (None, [], {"US": "美国"}, {"US": {"zh-CN": 5}}):
            with self.subTest(names=names):
                self.assertEqual(places.name("US", names), "US")
        self.assertEqual([places.name(location, self.NAMES) for location in ("", "  ", None)], ["", "", ""])


class PlaceForTests(SimpleTestCase):
    def test_a_city(self):
        self.assertEqual(place_for(NETWORKS["223.5.5.0/24"]), (IN_HANGZHOU, {
            "CN": {"en": "China", "zh-CN": "中国"}, "1784764": {"en": "Zhejiang", "zh-CN": "浙江"}, "1808926": {"en": "Hangzhou", "zh-CN": "杭州"},
        }))

    def test_only_the_first_subdivision(self):
        location, names = place_for(NETWORKS["81.2.69.0/24"])  # England, and Suffolk (2636561) in it
        self.assertEqual((location, sorted(names)), (IN_IPSWICH, ["2646057", "6269131", "GB"]))

    def test_less_than_a_city(self):
        self.assertEqual(place_for(city(CHINA)), ("CN", {"CN": {"en": "China", "zh-CN": "中国"}}))
        self.assertEqual(place_for(city(CHINA, ZHEJIANG))[0], "CN 1784764")
        self.assertEqual(place_for(city(CHINA, city=HANGZHOU))[0], "CN 1808926")
        # Only the country the network is registered in, as for anycast networks (1.1.1.1).
        self.assertEqual(place_for(NETWORKS["45.83.0.0/16"]), ("NL", {"NL": {"en": "Netherlands", "zh-CN": "荷兰"}}))

    def test_nothing_known(self):
        for record in (None, {}, {"continent": place(6255147, "Asia", "亚洲", code="AS")}):
            with self.subTest(record=record):
                self.assertEqual(place_for(record), (UNKNOWN, {}))

    def test_records_of_an_unexpected_shape(self):
        # The file is downloaded: nothing about a record is taken for granted.
        for record in ("CN", ["CN"], {"country": "CN"}, {"country": {"iso_code": 86}}, {"country": {"names": {"zh-CN": "中国"}}},
                       {"country": {"iso_code": "CHN"}}, {"country": {"iso_code": "C1"}}):
            with self.subTest(record=record):
                self.assertEqual(place_for(record)[0], UNKNOWN)
        for parts in ({"subdivisions": "Zhejiang"}, {"subdivisions": []}, {"subdivisions": [None]}, {"city": "Hangzhou"},
                      {"city": {"geoname_id": "1808926"}}, {"city": {"geoname_id": True}}, {"city": {"geoname_id": 0}},
                      {"city": {"geoname_id": -5}}, {"city": {"geoname_id": 10**10}}, {"city": {"geoname_id": 1.5}}):
            with self.subTest(parts=parts):
                self.assertEqual(place_for({"country": CHINA, **parts})[0], "CN")

    def test_what_is_kept_of_the_text(self):
        record = {"country": {"iso_code": " us ", "names": {"de": "Vereinigte Staaten", "en": "United\nStates\x1b", "zh-CN": "美国" * 80}},
                  "city": {"geoname_id": 9_999_999_999, "names": {"en": 5, "zh-CN": " "}}}
        self.assertEqual(place_for(record), ("US 9999999999", {"US": {"en": "UnitedStates", "zh-CN": ("美国" * 80)[:places.NAME_MAX]}}))
        longest = {"country": CHINA, "subdivisions": [{"geoname_id": 9_999_999_999}], "city": {"geoname_id": 9_999_999_998}}
        self.assertLessEqual(len(place_for(longest)[0]), LOCATION_MAX)


class DatabaseFileTestCase(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.folder = Path(tempfile.mkdtemp(prefix="hfile-test-geoip-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def write(self, data, name="test.mmdb"):
        path = self.folder / name
        path.write_bytes(data)
        return path


class LocatorTests(DatabaseFileTestCase):
    def setUp(self):
        super().setUp()
        self.find = Locator(open_reader(self.write(sample_database())))

    def test_locations(self):
        cases = {
            "1.2.4.8": "CN",
            "223.5.5.5": IN_HANGZHOU,
            "36.110.1.1": IN_BEIJING,
            "8.8.8.8": "US",
            "168.95.1.1": "TW",
            "1.160.0.1": IN_TAIPEI,
            "202.67.240.1": IN_HONG_KONG,
            "202.175.3.3": "MO",
            "81.2.69.142": IN_IPSWICH,
            "45.83.0.1": "NL",  # only the registered country, as for anycast networks
            "77.88.1.1": IN_MOSCOW,
            "9.9.9.9": "ZZ",
            "5.5.5.5": UNKNOWN,
            "2400:3200::1": IN_HANGZHOU,
            "2001:4860:4860::8888": IN_MOUNTAIN_VIEW,
            "::ffff:8.8.8.8": "US",  # an IPv4-mapped address is looked up as IPv4
            "2a00::1": UNKNOWN,
        }
        self.assertEqual({ip: self.find(ip) for ip in cases}, cases)

    def test_private_addresses_never_reach_the_database(self):
        for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "100.64.0.1", "169.254.1.1", "203.0.113.7", "::1", "fe80::1%eth0", "fd00::1"):
            with self.subTest(ip=ip):
                self.assertEqual(self.find.place(ip), ("PRIVATE", {}))

    def test_not_an_address(self):
        for value in ("", None, "nope", "1.2.3", "1.2.3.4/24"):
            self.assertEqual(self.find.place(value), ("", {}))

    def test_a_damaged_file_gives_nothing_rather_than_failing(self):
        class Broken:
            def get(self, address):
                raise maxminddb.InvalidDatabaseError("bad pointer")

        with self.assertLogs("apps.ipgeo.lookup", "WARNING"):
            self.assertEqual(Locator(Broken()).place("8.8.8.8"), ("", {}))


class OpenReaderTests(DatabaseFileTestCase):
    def test_a_city_database(self):
        meta = open_reader(self.write(sample_database(build_epoch=1_762_302_000))).metadata()
        self.assertEqual((meta.database_type, meta.ip_version, meta.build_epoch), ("GeoLite2-City", 6, 1_762_302_000))

    def test_files_that_are_not_one(self):
        whole = sample_database()
        cases = {
            "不是 MaxMind 格式的数据库": b"<html>Not Found</html>" * 100,
            "不是 MaxMind 格式的数据库，或者文件坏了": whole[: len(whole) // 2],  # cut short: no metadata at the end
            "不是 GeoLite2-City 这样的城市数据库（类型 GeoLite2-ASN": build_mmdb({"8.8.8.0/24": {"autonomous_system_number": 15169}}, database_type="GeoLite2-ASN"),
            "不是 GeoLite2-City 这样的城市数据库（类型 GeoLite2-Country": build_mmdb({"8.8.8.0/24": {"country": CHINA}}, database_type="GeoLite2-Country"),
            "不是 GeoLite2-City 这样的城市数据库（类型 GeoLite2-City，IPv4）": build_mmdb({"8.8.8.0/24": city(CHINA)}, ip_version=4),
        }
        for message, data in cases.items():
            with self.subTest(message=message), self.assertRaisesMessage(lookup.DatabaseProblem, message):
                open_reader(self.write(data))
        with self.assertRaisesMessage(lookup.DatabaseProblem, "找不到数据库文件"):
            open_reader(self.folder / "missing.mmdb")


class LocateTests(GeoTestMixin, TestCase):
    def test_off_and_on(self):
        self.install(enabled=False)
        self.assertEqual(lookup.locate("223.5.5.5"), "")
        self.turn(True)
        self.assertEqual((lookup.locate("223.5.5.5"), lookup.locate("8.8.8.8")), (IN_HANGZHOU, "US"))
        self.assertEqual(lookup.locate(None), "")

    def test_on_without_a_database(self):
        self.turn(True)
        self.assertEqual(lookup.locate("8.8.8.8"), "")

    def test_a_file_that_went_missing(self):
        self.install()
        (self.folder / FILE_NAME).unlink()
        with self.assertLogs("apps.ipgeo.lookup", "WARNING") as logs:
            self.assertEqual(lookup.locate("8.8.8.8"), "")
            self.assertEqual(lookup.locate("8.8.8.8"), "")
        self.assertEqual(len(logs.records), 1)  # once per download, not once per lookup
        self.install()  # downloaded again
        self.assertEqual(lookup.locate("8.8.8.8"), "US")

    def test_a_new_download_is_read_again(self):
        self.install()
        self.assertEqual(lookup.locate("8.8.8.8"), "US")
        self.install(build_mmdb({"8.8.8.0/24": city(place(1, "Elsewhere", iso_code="XX"))}))
        self.assertEqual(lookup.locate("8.8.8.8"), "XX")

    def test_any_error_in_a_lookup_gives_nothing(self):
        self.install()
        with mock.patch.object(Locator, "place", side_effect=RuntimeError("boom")):
            with self.assertLogs("apps.ipgeo.lookup", "ERROR"):
                self.assertEqual(lookup.locate("8.8.8.8"), "")

    def test_the_settings_and_names_are_read_once_per_request(self):
        self.install()
        lookup.locate("223.5.5.5")
        token = net.bind_request(RequestFactory().get("/"))
        self.addCleanup(net.unbind_request, token)
        with CaptureQueriesContext(connection) as queries:
            for _ in range(5):
                self.assertEqual(lookup.locate("223.5.5.5"), IN_HANGZHOU)
                self.assertEqual(lookup.shown(IN_HANGZHOU), "中国 浙江 杭州")
        reads = [query["sql"] for query in queries.captured_queries if query["sql"].lstrip().upper().startswith("SELECT")]
        self.assertEqual(sum("ipgeo_ipgeosettings" in sql for sql in reads), 1)
        self.assertEqual(sum("ipgeo_placename" in sql for sql in reads), 1)


class RememberTests(GeoTestMixin, TestCase):
    """The names come with the lookups: kept by code, once per place and process."""

    def test_a_lookup_keeps_the_names_of_the_places_it_found(self):
        self.install()
        for ip in ("223.5.5.5", "2001:4860:4860::8888", "192.168.1.1", "5.5.5.5", "77.88.1.1", "9.9.9.9"):
            lookup.locate(ip)
        self.assertEqual(dict(PlaceName.objects.values_list("code", "names")), {
            "CN": {"en": "China", "zh-CN": "中国"}, "1784764": {"en": "Zhejiang", "zh-CN": "浙江"}, "1808926": {"en": "Hangzhou", "zh-CN": "杭州"},
            "US": {"en": "United States", "zh-CN": "美国"}, "5332921": {"en": "California", "zh-CN": "加利福尼亚州"},
            "5375480": {"en": "Mountain View", "zh-CN": "山景城"}, "RU": {"en": "Russia"}, "524901": {"en": "Moscow"},
        })
        self.assertEqual([lookup.shown(location) for location in (IN_HANGZHOU, IN_MOUNTAIN_VIEW, IN_MOSCOW, "PRIVATE", UNKNOWN, "ZZ")],
                         ["中国 浙江 杭州", "美国 加利福尼亚州 山景城", "Russia Moscow", "内网", "未知", "ZZ"])
        with translation.override("en"):
            self.assertEqual(lookup.shown(IN_MOUNTAIN_VIEW), "Mountain View, California, United States")

    def test_once_per_place_and_process_and_again_when_they_change(self):
        with self.captureOnCommitCallbacks(execute=True):  # taken for saved once committed
            lookup.remember({"US": {"en": "United States"}})
        with CaptureQueriesContext(connection) as queries:
            lookup.remember({"US": {"en": "United States"}})
        self.assertFalse([q for q in queries.captured_queries if "ipgeo_placename" in q["sql"]])
        lookup.remember({"US": {"en": "USA"}, "": {"en": "x"}, "NL": {}})
        self.assertEqual(dict(PlaceName.objects.values_list("code", "names")), {"US": {"en": "USA"}})

    def test_a_rollback_takes_them_back(self):
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ZeroDivisionError), transaction.atomic():
                lookup.remember({"US": {"en": "United States"}})
                1 / 0
        self.assertFalse(PlaceName.objects.exists())
        with CaptureQueriesContext(connection) as queries:
            lookup.remember({"US": {"en": "United States"}})  # not taken for saved: written again
        self.assertTrue([q for q in queries.captured_queries if "ipgeo_placename" in q["sql"]])
        self.assertTrue(PlaceName.objects.filter(code="US").exists())

    def test_a_database_error_doesnt_stop_a_lookup(self):
        self.install()
        with mock.patch.object(PlaceName.objects, "bulk_create", side_effect=lookup.DatabaseError("locked")):
            with self.assertLogs("apps.ipgeo.lookup", "WARNING"):
                self.assertEqual(lookup.locate("223.5.5.5"), IN_HANGZHOU)
        self.assertEqual(lookup.shown(IN_HANGZHOU), "CN")  # no names yet: the country's code


# ------------------------------------------------------------------ stored with the IPs


class RecordingTests(GeoTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")

    def login(self, address, password="pw-alice-2026"):
        return APIClient(REMOTE_ADDR=address).post(SESSION, {"username": "alice", "password": password}, format="json")

    def test_logging_in(self):
        self.install()
        self.assertEqual(self.login("2400:3200::1").status_code, 201)
        self.alice.refresh_from_db()
        self.assertEqual((self.alice.last_login_ip, self.alice.last_login_location), ("2400:3200::1", IN_HANGZHOU))
        self.assertEqual(ActivityLog.objects.get().ip_location, IN_HANGZHOU)
        self.turn(False)
        self.assertEqual(self.login("8.8.8.8").status_code, 201)
        self.alice.refresh_from_db()
        self.assertEqual((self.alice.last_login_ip, self.alice.last_login_location), ("8.8.8.8", ""))
        self.assertEqual(ActivityLog.objects.latest("id").ip_location, "")

    def test_failed_logins(self):
        self.install()
        self.assertEqual(self.login("168.95.1.1", password="wrong-2026").status_code, 400)
        self.assertEqual((ActivityLog.objects.get().kind, ActivityLog.objects.get().ip_location), ("login_failed", "TW"))

    def test_a_file_maxminddb_chokes_on_doesnt_stop_logins(self):
        self.install()
        with mock.patch.object(lookup.maxminddb, "open_database", side_effect=TypeError("bad metadata")) as opened:
            with self.assertLogs("apps.ipgeo.lookup", "WARNING"):
                for _ in range(3):
                    self.assertEqual(self.login("8.8.8.8").status_code, 201)
        self.assertEqual(opened.call_count, 1)  # remembered as unusable until the next download
        self.assertEqual(set(ActivityLog.objects.values_list("ip_location", flat=True)), {""})


class DownloadLogTests(GeoTestMixin, LinksTestCase):
    def test_a_short_link(self):
        self.install()
        self.login()
        link = self.short()
        response = self.visitor().get(f"/s/{link.key}", REMOTE_ADDR="202.67.240.1")
        self.assertEqual(response.status_code, 302, response.content[:300])
        self.assertEqual((DownloadLog.objects.get().ip, DownloadLog.objects.get().ip_location), ("202.67.240.1", IN_HONG_KONG))


class ShownTests(GeoTestMixin, LinksTestCase):
    """Where the admin's pages show the stored locations: /manage/ (through the API) and the Django admin."""

    def setUp(self):
        super().setUp()
        self.install()
        self.admin, self.client = admin_client(last_login=timezone.now(), last_login_ip="8.8.8.8", last_login_location="US")
        for ip in ("8.8.8.8", "2001:4860:4860::8888", "202.175.3.3"):
            lookup.locate(ip)  # their names, as the lookups that stored the locations kept them
        ActivityLog.objects.create(kind="login", actor=self.admin, actor_name="boss", ip="2001:4860:4860::8888", ip_location=IN_MOUNTAIN_VIEW)
        DownloadLog.objects.create(kind="short", storage=self.storage, path="/top.txt", short_key="Ab3dE7fG", ip="202.175.3.3",
                                   ip_location="MO")

    def located(self):
        users = self.client.get("/api/v1/manage/users/").data["results"]
        return (
            next(row for row in users if row["username"] == "boss")["last_login_location"],
            self.client.get(f"/api/v1/manage/users/{self.admin.pk}/").data["last_login_location"],
            self.client.get("/api/v1/manage/activity/").data["results"][0]["ip_location"],
            self.client.get("/api/v1/manage/download-logs/").data["results"][0]["ip_location"],
        )

    def test_the_manage_api(self):
        self.assertEqual(self.located(), ("美国", "美国", "美国 加利福尼亚州 山景城", "中国澳门"))
        self.assertEqual(self.client.get("/api/v1/manage/activity/", HTTP_ACCEPT_LANGUAGE="en").data["results"][0]["ip_location"],
                         "Mountain View, California, United States")

    def test_turned_off_they_are_kept_but_not_shown(self):
        self.turn(False)
        self.assertEqual(self.located(), ("", "", "", ""))
        self.assertEqual(ActivityLog.objects.get().ip_location, IN_MOUNTAIN_VIEW)

    def test_the_settings_are_read_once_for_a_whole_list(self):
        for _ in range(5):
            ActivityLog.objects.create(kind="login", actor_name="boss", ip="2001:4860:4860::8888", ip_location=IN_MOUNTAIN_VIEW)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get("/api/v1/manage/activity/").status_code, 200)
        self.assertEqual(sum("ipgeo_ipgeosettings" in query["sql"] for query in queries.captured_queries), 1)
        self.assertEqual(sum("ipgeo_placename" in query["sql"] for query in queries.captured_queries), 1)

    def test_the_django_admin(self):
        client = self.client_class()
        client.force_login(User.objects.create_superuser("root", password="pw-root-2026"))
        for path in ("accounts/user/", f"accounts/user/{self.admin.pk}/change/"):
            with self.subTest(path=path):
                self.assertContains(client.get(admin_url(path)), "8.8.8.8（美国）")
        self.turn(False)
        page = client.get(admin_url("accounts/user/")).content.decode()
        self.assertIn("8.8.8.8", page)
        self.assertNotIn("8.8.8.8（美国）", page)


# ------------------------------------------------------------------ the API


class ApiTests(GeoTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.run_jobs_in_place()
        self.admin, self.admin_client = admin_client()
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")

    def test_admins_only(self):
        calls = [
            ("get", API, None),
            ("patch", API, {"enabled": True}),
            ("post", API + "download/", {}),
            ("post", API + "fill/", {}),
            ("get", API + "job/", None),
            ("get", API + "check/?ip=8.8.8.8", None),
        ]
        for method, url, body in calls:
            with self.subTest(url=url, method=method):
                client = APIClient()
                self.assertIn(getattr(client, method)(url, body, format="json").status_code, (401, 403))
                client.force_authenticate(self.alice)
                self.assertEqual(getattr(client, method)(url, body, format="json").status_code, 403)
        self.assertFalse(IpGeoSettings.objects.filter(enabled=True).exists())

    def test_the_state_before_anything(self):
        r = self.admin_client.get(API)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            {key: r.data[key] for key in ("enabled", "database", "job", "folder")},
            {"enabled": False, "database": None, "job": None, "folder": str(self.folder)},
        )
        self.assertEqual([part["key"] for part in r.data["unfilled"]["parts"]], ["users", "activity", "downloads"])

    def test_turning_it_on_and_off(self):
        r = self.admin_client.patch(API, {"enabled": True}, format="json")
        self.assertEqual((r.status_code, r.data["enabled"]), (200, True))
        self.assertTrue(IpGeoSettings.load().enabled)
        self.assertFalse(self.admin_client.patch(API, {"enabled": False}, format="json").data["enabled"])
        for body in ({}, {"enabled": "maybe"}):
            self.assertEqual(self.admin_client.patch(API, body, format="json").status_code, 400)
        # A form without the field doesn't turn it off.
        self.turn(True)
        self.assertEqual(self.admin_client.patch(API, {"other": "x"}, format="multipart").status_code, 400)
        self.assertTrue(IpGeoSettings.load().enabled)

    def test_the_database_in_use(self):
        data = sample_database()
        self.install(data)
        database = self.admin_client.get(API).data["database"]
        self.assertEqual(
            {key: database[key] for key in ("file", "size", "sha256", "missing")},
            {"file": FILE_NAME, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "missing": False},
        )
        (self.folder / FILE_NAME).unlink()
        self.assertTrue(self.admin_client.get(API).data["database"]["missing"])

    def test_checking_an_address(self):
        self.turn(True)
        self.assertContains(self.admin_client.get(API + "check/?ip=8.8.8.8"), "还没有能用的数据库", status_code=409)
        self.install()
        self.assertEqual(self.admin_client.get(API + "check/?ip=223.5.5.5").data, {"ip": "223.5.5.5", "code": IN_HANGZHOU, "location": "中国 浙江 杭州"})
        self.assertEqual(self.admin_client.get(API + "check/?ip=::ffff:8.8.8.8").data, {"ip": "8.8.8.8", "code": "US", "location": "美国"})
        self.assertEqual(self.admin_client.get(API + "check/?ip=192.168.1.1").data["location"], "内网")
        self.assertEqual(self.admin_client.get(API + "check/?ip=223.5.5.5", HTTP_ACCEPT_LANGUAGE="en").data["location"], "Hangzhou, Zhejiang, China")
        for bad in ("", "nope", "1" * 65):
            self.assertEqual(self.admin_client.get(API + "check/", {"ip": bad}).status_code, 400)
        self.assertEqual(self.admin_client.get(API + "check/").status_code, 400)
        self.turn(False)
        self.assertContains(self.admin_client.get(API + "check/?ip=8.8.8.8"), "先启用", status_code=409)

    def test_the_names_of_a_check_whose_names_couldnt_be_saved(self):
        self.install()
        with mock.patch.object(PlaceName.objects, "bulk_create", side_effect=lookup.DatabaseError("locked")):
            with self.assertLogs("apps.ipgeo.lookup", "WARNING"):
                r = self.admin_client.get(API + "check/?ip=81.2.69.142")
        self.assertEqual(r.data["location"], "英国 英格兰 伊普斯威奇")  # from the record itself


class DownloadTests(GeoTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.run_jobs_in_place()
        self.admin, self.admin_client = admin_client()
        self.turn(True)

    def download(self, **extra):
        return self.admin_client.post(API + "download/", {}, format="json", **extra)

    def job(self):
        return self.admin_client.get(API + "job/").data["job"]

    def test_downloading(self):
        data = sample_database(build_epoch=1_762_302_000)  # 2025-11-05 00:20 UTC
        server = self.serve({URL: data})
        (self.folder / ".download-old.part").write_bytes(b"left by a job that died")
        r = self.download()
        self.assertEqual(r.status_code, 202, r.data)
        self.assertEqual(server.asked, [URL])
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), [FILE_NAME])
        self.assertEqual((self.folder / FILE_NAME).read_bytes(), data)
        config = IpGeoSettings.load()
        self.assertEqual(
            (config.file_name, config.sha256, config.size, config.built_at.timestamp()),
            (FILE_NAME, hashlib.sha256(data).hexdigest(), len(data), 1_762_302_000),
        )
        job = self.job()
        self.assertEqual((job["kind"], job["state"], job["done"], job["total"]), ("download", "done", len(data), len(data)))
        self.assertEqual(job["message"], "下载好了：数据日期 2025-11-05。")
        self.assertEqual(self.admin_client.get(API + "check/?ip=2001:4860:4860::8888").data["location"], "美国 加利福尼亚州 山景城")
        self.assertEqual(PlaceName.objects.get(code="5375480").names, {"en": "Mountain View", "zh-CN": "山景城"})
        # In the activity log as the admin who started it did it, from where they did.
        entry = ActivityLog.objects.get()
        self.assertEqual((entry.kind, entry.actor, entry.ip, entry.detail),
                         ("download_ip_geo", self.admin, "127.0.0.1", {"built_at": "2025-11-05T00:20:00+00:00"}))

    def test_the_answer_comes_at_once(self):
        # The job runs in the background; what the POST answers is the state when it started.
        self.install()
        with mock.patch.object(jobs, "start_thread") as start:
            r = self.download()
        self.assertEqual(r.status_code, 202)
        self.assertEqual((r.data["job"]["kind"], r.data["job"]["state"]), ("download", "running"))
        self.assertEqual(start.call_args.args[:2], (jobs.download, r.data["job"]["id"]))
        self.assertEqual(start.call_args.args[2]["actor"], self.admin)  # who started it, for the activity log
        self.assertContains(self.download(), "正在下载数据库，等它做完再试。", status_code=409)
        self.assertContains(self.admin_client.post(API + "fill/", {}, format="json"), "正在下载数据库", status_code=409)

    def test_updating_when_nothing_is_new(self):
        data = sample_database()
        self.install(data)
        before = IpGeoSettings.load()
        mtime = (self.folder / FILE_NAME).stat().st_mtime_ns
        self.serve({URL: data})
        self.download()
        self.assertTrue(self.job()["message"].startswith("已经是最新的了"), self.job())
        self.assertEqual((self.folder / FILE_NAME).stat().st_mtime_ns, mtime)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), [FILE_NAME])  # the download is gone
        self.assertEqual(IpGeoSettings.load().downloaded_at, before.downloaded_at)
        self.assertFalse(ActivityLog.objects.filter(kind="download_ip_geo").exists())  # nothing new: nothing to log

    def test_newer_data(self):
        self.install()
        self.assertEqual(lookup.locate("8.8.8.8"), "US")
        newer = build_mmdb({"8.8.8.0/24": city(UNITED_KINGDOM, ENGLAND, city=IPSWICH)}, build_epoch=1_763_000_000)
        self.serve({URL: newer})
        self.download()
        self.assertEqual((self.folder / FILE_NAME).read_bytes(), newer)
        self.assertEqual(lookup.locate("8.8.8.8"), IN_IPSWICH)  # read again without a restart

    def test_a_damaged_local_file_is_replaced_by_the_same_download(self):
        data = sample_database()
        self.install(data)
        (self.folder / FILE_NAME).write_bytes(b"damaged")
        self.serve({URL: data})
        self.download()
        config = IpGeoSettings.load()
        self.assertEqual(config.job_state, "done", config.job_message)
        self.assertNotIn("已经是最新的", config.job_message)
        self.assertEqual((self.folder / FILE_NAME).read_bytes(), data)

    def test_in_the_language_of_who_started_it(self):
        self.serve({URL: sample_database(build_epoch=1_762_302_000)})
        self.download(HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(IpGeoSettings.load().job_message, "Downloaded: data of 2025-11-05.")

    def assertFailed(self, message):
        job = self.job()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn(message, job["message"])
        self.assertEqual(list(self.folder.iterdir()), [])  # nothing left behind
        self.assertEqual(IpGeoSettings.load().file_name, "")

    def test_a_file_that_is_not_a_database(self):
        self.serve({URL: b"\x00" * 5000})
        self.download()
        self.assertFailed("下载的文件用不了：不是 MaxMind 格式的数据库")

    def test_a_country_database(self):
        self.serve({URL: build_mmdb({"8.8.8.0/24": {"country": CHINA}}, database_type="GeoLite2-Country")})
        self.download()
        self.assertFailed("下载的文件用不了：不是 GeoLite2-City 这样的城市数据库")

    def test_cut_short_without_a_length(self):
        # ip.adysec.com doesn't say how long the file is: a file cut short is found out when it is opened.
        data = sample_database()
        self.serve({URL: data[: len(data) // 2]}, length=False)
        self.download()
        self.assertFailed("下载的文件用不了：不是 MaxMind 格式的数据库，或者文件坏了")

    def test_cut_short(self):
        class Short(FakeResponse):
            def __init__(self, body, length=True):
                super().__init__(body[:100], length)
                self.headers = {"Content-Length": str(len(body))}

        data = sample_database()
        with mock.patch.object(jobs, "fetch", lambda request: Short(data)):
            self.download()
        self.assertFailed("下载不完整")

    def test_too_large(self):
        self.serve({URL: sample_database()})
        with mock.patch.object(jobs, "MAX_BYTES", 1000):
            self.download()
        self.assertFailed("超过了 0 MB 的上限，没有下载")

    def test_too_large_without_a_length(self):
        self.serve({URL: sample_database()}, length=False)
        with mock.patch.object(jobs, "MAX_BYTES", 1000), mock.patch.object(jobs, "CHUNK", 600):
            self.download()
        self.assertFailed("文件超过了 0 MB 的上限，停止下载")

    def test_network_problems(self):
        cases = {
            "ip.adysec.com 回答 404": {},
            "连不上 ip.adysec.com（[Errno -2] Name or service not known）": {URL: urllib.error.URLError(OSError(-2, "Name or service not known"))},
            "连不上 ip.adysec.com（30 秒没有回应）": {URL: urllib.error.URLError(TimeoutError())},
        }
        for message, files in cases.items():
            with self.subTest(message=message):
                self.serve(files)
                self.download()
                self.assertFailed(message)

    def test_a_stalled_download(self):
        class Stalled(FakeResponse):
            def read(self, size=-1):
                raise TimeoutError

            read1 = read  # what jobs._read uses

        with mock.patch.object(jobs, "fetch", lambda request: Stalled(sample_database())):
            self.download()
        self.assertFailed("30 秒没有收到数据")

    def test_only_while_turned_on(self):
        self.turn(False)
        self.assertContains(self.download(), "先启用 IP 归属地", status_code=409)


class RedirectTests(SimpleTestCase):
    def follow(self, url):
        handler = jobs._SourceRedirects()
        request = urllib.request.Request(URL)
        return handler.redirect_request(request, None, 302, "Found", {}, url)

    def test_only_to_the_same_host_over_https(self):
        self.assertIsNone(self.follow("https://example.com/GeoLite2-City.mmdb"))
        self.assertIsNone(self.follow("http://ip.adysec.com/geolite/GeoLite2-City.mmdb"))
        self.assertIsNone(self.follow("ftp://ip.adysec.com/geolite/GeoLite2-City.mmdb"))
        self.assertIsNone(self.follow("https://ip.adysec.com.example.com/geolite/GeoLite2-City.mmdb"))
        self.assertIsNotNone(self.follow("https://ip.adysec.com/geolite/GeoLite2-City.mmdb?v=2"))


class JobStateTests(GeoTestMixin, TestCase):
    def test_one_job_at_a_time(self):
        config = jobs.claim(IpGeoSettings.Job.DOWNLOAD)
        with self.assertRaisesMessage(jobs.Busy, "正在下载数据库"):
            jobs.claim(IpGeoSettings.Job.FILL)
        # A job that stopped moving (the program was restarted) no longer counts.
        IpGeoSettings.objects.filter(pk=1).update(job_updated_at=timezone.now() - jobs.STALE - timedelta(seconds=1))
        stale = IpGeoSettings.load()
        self.assertEqual(jobs.job_status(stale)["state"], "failed")
        self.assertIn("中途重启", jobs.job_status(stale)["message"])
        newer = jobs.claim(IpGeoSettings.Job.FILL)
        self.assertNotEqual(newer.job_id, config.job_id)
        # The old job, should it still be alive, notices and stops without writing anything.
        with self.assertRaises(jobs.Superseded):
            jobs.Progress(config.job_id).tick(done=5, force=True)
        jobs.run(lambda progress: "旧的完成了", config.job_id)
        self.assertEqual(IpGeoSettings.load().job_state, "running")

    def test_a_download_that_was_taken_over_replaces_nothing(self):
        self.turn(True)
        config = jobs.claim(IpGeoSettings.Job.DOWNLOAD)
        real = lookup.open_reader

        def taken_over(path):
            IpGeoSettings.objects.filter(pk=1).update(job_id="another")  # a newer job, while this one checks the file
            return real(path)

        with mock.patch.object(jobs, "fetch", FakeServer({URL: sample_database()})), mock.patch.object(lookup, "open_reader", taken_over):
            jobs.run(jobs.download, config.job_id)
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual((IpGeoSettings.load().job_state, IpGeoSettings.load().file_name), ("running", ""))

    def test_unexpected_errors_end_the_job(self):
        config = jobs.claim(IpGeoSettings.Job.FILL)

        def work(progress):
            raise ZeroDivisionError("division by zero")

        with self.assertLogs("apps.ipgeo.jobs", "ERROR"):
            jobs.run(work, config.job_id)
        config.refresh_from_db()
        self.assertEqual((config.job_state, config.job_message), ("failed", "出错了：division by zero"))


class FillTests(GeoTestMixin, LinksTestCase):
    def setUp(self):
        super().setUp()
        self.run_jobs_in_place()
        self.boss, self.admin_client = admin_client()
        self.turn(True)
        log = ActivityLog.objects
        self.blank_us = log.create(kind="login_failed", target_name="x", ip="8.8.8.8")
        self.blank_cn = log.create(kind="login_failed", target_name="x", ip="1.2.4.8")
        self.blank_lan = log.create(kind="login_failed", target_name="x", ip="192.168.1.1")
        self.filled = log.create(kind="login_failed", target_name="x", ip="8.8.8.8", ip_location="OLD")
        self.no_ip = log.create(kind="login_failed", target_name="x", ip=None)
        User.objects.filter(pk=self.alice.pk).update(last_login_ip="2400:3200::1")
        self.download = DownloadLog.objects.create(kind="page", storage=self.storage, path="/top.txt", ip="202.175.3.3")

    def location(self, row):
        row.refresh_from_db()
        return row.last_login_location if isinstance(row, User) else row.ip_location

    def test_filling_in(self):
        self.install()
        self.assertEqual(self.admin_client.get(API).data["unfilled"]["total"], 5)
        r = self.admin_client.post(API + "fill/", {}, format="json")
        self.assertEqual(r.status_code, 202, r.data)
        self.assertEqual(
            [self.location(row) for row in (self.blank_us, self.blank_cn, self.blank_lan, self.filled, self.no_ip, self.alice, self.download)],
            ["US", "CN", "PRIVATE", "OLD", "", IN_HANGZHOU, "MO"],
        )
        job = self.admin_client.get(API + "job/").data["job"]
        self.assertEqual((job["state"], job["message"], job["done"], job["total"]), ("done", "补全了 5 条记录的归属地。", 5, 5))
        self.assertEqual(sorted(PlaceName.objects.values_list("code", flat=True)), ["1784764", "1808926", "CN", "MO", "US"])
        self.assertEqual(self.admin_client.get(API).data["unfilled"]["total"], 0)
        filled = ActivityLog.objects.get(kind="fill_ip_geo")
        self.assertEqual((filled.actor, filled.detail, filled.ip_location), (self.boss, {"changed": 5, "redo": False, "addresses": 5}, "PRIVATE"))
        # Again: nothing to do, nothing to log.
        self.admin_client.post(API + "fill/", {}, format="json")
        self.assertEqual(self.admin_client.get(API + "job/").data["job"]["message"], "没有要补全的记录。")
        self.assertEqual(ActivityLog.objects.filter(kind="fill_ip_geo").count(), 1)

    def test_looking_everything_up_again(self):
        self.install()
        self.admin_client.post(API + "fill/", {}, format="json")
        self.install(build_mmdb({"8.8.8.0/24": city(place(4030656, "U.S. Outlying Islands", "美国本土外小岛屿", iso_code="UM"))}))
        self.admin_client.post(API + "fill/", {"redo": True}, format="json")
        self.assertEqual([self.location(row) for row in (self.blank_us, self.filled, self.blank_cn, self.alice)], ["UM", "UM", UNKNOWN, UNKNOWN])
        # 6 addresses (the first 补全's entry in the activity log has the admin's); the two rows of 8.8.8.8,
        # 1.2.4.8, Alice's login and the download changed, the private addresses stayed PRIVATE.
        job = self.admin_client.get(API + "job/").data["job"]
        self.assertEqual(job["message"], "重新查了 6 个 IP，5 条记录的归属地有变化。")
        self.assertEqual(ActivityLog.objects.filter(kind="fill_ip_geo").latest("id").detail, {"changed": 5, "redo": True, "addresses": 6})

    def test_an_ip_in_two_tables_is_looked_up_once(self):
        self.install()
        User.objects.filter(pk=self.alice.pk).update(last_login_ip="8.8.8.8")
        self.admin_client.post(API + "fill/", {"redo": True}, format="json")
        config = IpGeoSettings.load()
        self.assertEqual((config.job_total, config.job_done), (4, 4), config.job_message)

    def test_only_with_a_database(self):
        self.assertContains(self.admin_client.post(API + "fill/", {}, format="json"), "还没有能用的数据库", status_code=409)
        self.install(enabled=False)
        self.assertContains(self.admin_client.post(API + "fill/", {}, format="json"), "先启用 IP 归属地", status_code=409)
        self.assertEqual(self.location(self.blank_us), "")


class ThreadTests(GeoTestMixin, TransactionTestCase):
    """The real background thread: it writes through its own database connection."""

    def test_a_download_in_the_background(self):
        self.turn(True)
        self.serve({URL: sample_database(build_epoch=1_762_302_000)})
        threads = []
        real_start = threading.Thread.start

        def start(thread):
            threads.append(thread)
            real_start(thread)

        with mock.patch.object(threading.Thread, "start", start), translation.override("en"):
            jobs.start_download()
        self.assertEqual([thread.name for thread in threads], ["ipgeo-job"])
        threads[0].join(30)
        self.assertFalse(threads[0].is_alive())
        config = IpGeoSettings.load()
        self.assertEqual((config.job_state, config.file_name), ("done", FILE_NAME))
        self.assertEqual(config.job_message, "Downloaded: data of 2025-11-05.")  # the starter's language
