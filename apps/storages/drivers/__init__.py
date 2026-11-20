"""The storage drivers by type (StorageSource.type), in the order the admin form lists them.

Each value is "module:Class"; the module is imported the first time the type is used, so a
driver whose library is missing only breaks storages of that type.
"""

from __future__ import annotations

import importlib

from django.utils.translation import gettext_lazy

from .base import StorageDriver

DRIVERS: dict[str, str] = {
    "local": "apps.storages.drivers.local:LocalDriver",
    "s3": "apps.storages.drivers.s3:S3Driver",
    "aliyun": "apps.storages.drivers.s3:AliyunDriver",
    "tencent": "apps.storages.drivers.s3:TencentDriver",
    "huawei": "apps.storages.drivers.s3:HuaweiDriver",
    "minio": "apps.storages.drivers.s3:MinIODriver",
    "qiniu": "apps.storages.drivers.qiniu:QiniuDriver",
    "doge-cloud": "apps.storages.drivers.dogecloud:DogeCloudDriver",
    "upyun": "apps.storages.drivers.upyun:UpYunDriver",
    "azure-blob": "apps.storages.drivers.azure_blob:AzureBlobDriver",
    "bunny-storage": "apps.storages.drivers.bunny:BunnyStorageDriver",
    "ftp": "apps.storages.drivers.ftp:FTPDriver",
    "sftp": "apps.storages.drivers.sftp:SFTPDriver",
    "webdav": "apps.storages.drivers.webdav:WebDAVDriver",
    "smb": "apps.storages.drivers.smb:SMBDriver",
    "onedrive": "apps.storages.drivers.microsoft:OneDriveDriver",
    "onedrive-china": "apps.storages.drivers.microsoft:OneDriveChinaDriver",
    "onedrive-app": "apps.storages.drivers.microsoft:OneDriveAppDriver",
    "onedrive-app-china": "apps.storages.drivers.microsoft:OneDriveAppChinaDriver",
    "sharepoint": "apps.storages.drivers.microsoft:SharePointDriver",
    "sharepoint-china": "apps.storages.drivers.microsoft:SharePointChinaDriver",
    "google-drive": "apps.storages.drivers.google:GoogleDriveDriver",
    "open115": "apps.storages.drivers.open115:Open115Driver",
    "alipan-open": "apps.storages.drivers.alipan_open:AlipanOpenDriver",
    "baidu-netdisk": "apps.storages.drivers.baidu_netdisk:BaiduNetdiskDriver",
    "open123": "apps.storages.drivers.open123:Open123Driver",
    "yandex-disk": "apps.storages.drivers.yandex:YandexDiskDriver",
    "dropbox": "apps.storages.drivers.dropbox:DropboxDriver",
    "github-repo": "apps.storages.drivers.github_repo:GitHubRepoDriver",
    "febbox": "apps.storages.drivers.febbox:FebBoxDriver",
    "openlist": "apps.storages.drivers.openlist:OpenListDriver",
    "seafile": "apps.storages.drivers.seafile:SeafileDriver",
    "kodbox": "apps.storages.drivers.kodbox:KodBoxDriver",
    "cloudreve": "apps.storages.drivers.cloudreve:CloudreveDriver",
    "cloudreve-v3": "apps.storages.drivers.cloudreve_v3:CloudreveV3Driver",
    "teldrive": "apps.storages.drivers.teldrive:TeldriveDriver",
    "misskey": "apps.storages.drivers.misskey:MisskeyDriver",
    "cf-imgbed": "apps.storages.drivers.cf_imgbed:ImgBedDriver",
    "ipfs": "apps.storages.drivers.ipfs:IPFSDriver",
    "emby": "apps.storages.drivers.emby:EmbyDriver",
    "github-releases": "apps.storages.drivers.github_releases:GitHubReleasesDriver",
    "cnb-releases": "apps.storages.drivers.cnb_releases:CNBReleasesDriver",
    "autoindex": "apps.storages.drivers.autoindex:AutoIndexDriver",
    "url-tree": "apps.storages.drivers.url_tree:URLTreeDriver",
    "123-link": "apps.storages.drivers.pan123_link:Pan123LinkDriver",
    "openlist-share": "apps.storages.drivers.openlist:OpenListShareDriver",
    "lenovo-nas-share": "apps.storages.drivers.lenovo_nas_share:LenovoNasShareDriver",
    "onedrive-share": "apps.storages.drivers.onedrive_share:OneDriveShareDriver",
}

# The groups of the admin form's list of types, in its order, and the types in each; a type in none (the tests'
# "fake") comes after them.
GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "servers": (gettext_lazy("Local and servers"), ("local", "ftp", "sftp", "webdav", "smb")),
    "objects": (gettext_lazy("Object storage"),
                ("s3", "aliyun", "tencent", "huawei", "minio", "qiniu", "doge-cloud", "upyun", "azure-blob", "bunny-storage")),
    "drives": (gettext_lazy("Cloud drives"),
               ("onedrive", "onedrive-china", "onedrive-app", "onedrive-app-china", "sharepoint", "sharepoint-china",
                "google-drive", "open115", "alipan-open", "baidu-netdisk", "open123",
                "yandex-disk", "dropbox", "github-repo", "febbox")),
    "selfhosted": (gettext_lazy("Self-hosted services"), ("openlist", "seafile", "kodbox", "cloudreve", "cloudreve-v3", "teldrive", "misskey", "cf-imgbed", "ipfs", "emby")),
    "shares": (gettext_lazy("Share links and release pages"), ("github-releases", "cnb-releases", "autoindex", "url-tree", "123-link", "openlist-share", "lenovo-nas-share",
                                                              "onedrive-share")),
}


def group_of(type_: str) -> str:
    """The key of the group of a type in GROUPS, or "" for none."""
    return next((key for key, (_, members) in GROUPS.items() if type_ in members), "")


class UnknownType(KeyError):
    pass


def driver_class(type_: str) -> type[StorageDriver]:
    try:
        target = DRIVERS[type_]
    except KeyError:
        raise UnknownType(type_) from None
    module_name, _, class_name = target.partition(":")
    cls = getattr(importlib.import_module(module_name), class_name)
    if cls.type != type_:
        raise RuntimeError(f"{target}.type is {cls.type!r}, not {type_!r}")
    return cls


def types() -> list[str]:
    return list(DRIVERS)
