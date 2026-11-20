"""Small MaxMind DB files for the tests: real files for the real reader, without downloading anything.

build_mmdb({"8.8.8.0/24": {"country": {...}}, ...}) writes the format as the specification describes it
(https://maxmind.github.io/MaxMind-DB/): a binary search tree with 24-bit records, IPv4 networks under
::/96 as in MaxMind's own files, then the data section and the metadata. Networks must not overlap.
"""

import ipaddress

METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"
CITY_TYPE = "GeoLite2-City"


class U16(int):
    """An integer stored as uint16 (the metadata wants exact types; libmaxminddb checks them)."""


class U64(int):
    """... as uint64."""


def _control(kind, size):
    if size < 29:
        first, extra = size, b""
    elif size < 29 + 256:
        first, extra = 29, bytes([size - 29])
    elif size < 285 + 65536:
        first, extra = 30, (size - 285).to_bytes(2, "big")
    else:
        first, extra = 31, (size - 65821).to_bytes(3, "big")
    if kind <= 7:
        return bytes([(kind << 5) | first]) + extra
    return bytes([first, kind - 7]) + extra  # an extended type: the type in the next byte


def encode(value):
    """One value of the data section: str, bool, non-negative int, dict or list."""
    if isinstance(value, bool):
        return _control(14, int(value))
    if isinstance(value, str):
        data = value.encode("utf-8")
        return _control(2, len(data)) + data
    if isinstance(value, int):
        if value < 0:
            raise ValueError("only unsigned integers")
        data = value.to_bytes((value.bit_length() + 7) // 8, "big")
        kind = 5 if isinstance(value, U16) else 9 if isinstance(value, U64) or value >= 2**32 else 6
        return _control(kind, len(data)) + data
    if isinstance(value, dict):
        return _control(7, len(value)) + b"".join(encode(str(key)) + encode(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return _control(11, len(value)) + b"".join(encode(item) for item in value)
    raise TypeError(type(value))


def _bits(network):
    network = ipaddress.ip_network(network)
    number = int(network.network_address)
    width = network.max_prefixlen
    bits = [(number >> (width - 1 - i)) & 1 for i in range(network.prefixlen)]
    if not bits:
        raise ValueError("a /0 network can't be stored")
    return ([0] * 96 + bits) if network.version == 4 else bits


def build_mmdb(networks, *, database_type=CITY_TYPE, ip_version=6, build_epoch=1_762_302_000, languages=("en", "zh-CN")):
    """The bytes of a database answering each network's addresses with its record."""
    nodes = [[None, None]]
    data = bytearray()
    for network, record in networks.items():
        offset = len(data)
        data += encode(record)
        bits = _bits(network)
        node = 0
        for depth, bit in enumerate(bits):
            child = nodes[node][bit]
            if depth == len(bits) - 1:
                if child is not None:
                    raise ValueError(f"{network} overlaps another network")
                nodes[node][bit] = ("data", offset)
                break
            if child is None:
                nodes.append([None, None])
                child = nodes[node][bit] = ("node", len(nodes) - 1)
            elif child[0] != "node":
                raise ValueError(f"{network} overlaps another network")
            node = child[1]
    count = len(nodes)

    def record_value(child):
        if child is None:
            return count  # nothing here
        kind, number = child
        return number if kind == "node" else count + 16 + number

    tree = b"".join(record_value(left).to_bytes(3, "big") + record_value(right).to_bytes(3, "big") for left, right in nodes)
    metadata = {
        "node_count": count,
        "record_size": U16(24),
        "ip_version": U16(ip_version),
        "database_type": database_type,
        "languages": list(languages),
        "binary_format_major_version": U16(2),
        "binary_format_minor_version": U16(0),
        "build_epoch": U64(build_epoch),
        "description": {"en": "HFile test database"},
    }
    return tree + bytes(16) + bytes(data) + METADATA_MARKER + encode(metadata)


def place(geoname_id, en=None, zh=None, **more):
    """A place of a record: {"geoname_id": ..., "names": {"en": ..., "zh-CN": ...}} and more such as iso_code."""
    found = {"geoname_id": geoname_id, **more}
    if en or zh:
        found["names"] = {key: value for key, value in (("en", en), ("zh-CN", zh)) if value}
    return found


def city(country, *subdivisions, city=None):
    """A record as GeoLite2-City has them: a country, its subdivisions from the largest, and the city."""
    record = {"country": country, "registered_country": country}
    if subdivisions:
        record["subdivisions"] = list(subdivisions)
    if city:
        record["city"] = city
    return record


# GeoLite2-City's own places (with their GeoNames IDs).
CHINA = place(1814991, "China", "中国", iso_code="CN")
UNITED_STATES = place(6252001, "United States", "美国", iso_code="US")
TAIWAN = place(1668284, "Taiwan", "台湾", iso_code="TW")
HONG_KONG = place(1819730, "Hong Kong", "香港", iso_code="HK")
MACAO = place(1821275, "Macao", "澳门", iso_code="MO")
UNITED_KINGDOM = place(2635167, "United Kingdom", "英国", iso_code="GB")
RUSSIA = place(2017370, "Russia", iso_code="RU")  # no Chinese name
ZHEJIANG, HANGZHOU = place(1784764, "Zhejiang", "浙江", iso_code="ZJ"), place(1808926, "Hangzhou", "杭州")
BEIJING_CITY, BEIJING = place(2038349, "Beijing", "北京市", iso_code="BJ"), place(1816670, "Beijing", "北京")
TAIPEI_CITY, TAIPEI = place(7280290, "Taipei City", "台北市", iso_code="TPE"), place(1668341, "Taipei", "台北市")
CALIFORNIA, MOUNTAIN_VIEW = place(5332921, "California", "加利福尼亚州", iso_code="CA"), place(5375480, "Mountain View", "山景城")
ENGLAND, SUFFOLK, IPSWICH = place(6269131, "England", "英格兰", iso_code="ENG"), place(2636561, "Suffolk", iso_code="SFK"), place(2646057, "Ipswich", "伊普斯威奇")

# Addresses the tests look up. All of them are public: private ones never reach the database.
NETWORKS = {
    "1.2.4.0/24": city(CHINA),
    "223.5.5.0/24": city(CHINA, ZHEJIANG, city=HANGZHOU),
    "36.110.0.0/16": city(CHINA, BEIJING_CITY, city=BEIJING),
    "8.8.8.0/24": city(UNITED_STATES),
    "168.95.0.0/16": city(TAIWAN),
    "1.160.0.0/12": city(TAIWAN, TAIPEI_CITY, city=TAIPEI),
    "202.67.240.0/20": city(HONG_KONG, city=place(1819729, "Hong Kong", "香港")),
    "202.175.0.0/16": city(MACAO),
    "81.2.69.0/24": city(UNITED_KINGDOM, ENGLAND, SUFFOLK, city=IPSWICH),
    "45.83.0.0/16": {"registered_country": place(2750405, "Netherlands", "荷兰", iso_code="NL")},  # an anycast network
    "77.88.0.0/18": city(RUSSIA, city=place(524901, "Moscow")),
    "9.9.9.0/24": city({"iso_code": "ZZ"}),  # a country without names
    "2400:3200::/32": city(CHINA, ZHEJIANG, city=HANGZHOU),
    "2001:4860::/32": city(UNITED_STATES, CALIFORNIA, city=MOUNTAIN_VIEW),
}


def sample_database(**kwargs):
    return build_mmdb(NETWORKS, **kwargs)
