"""The /manage/ pages of the settings: drawn from the serializers' fields, the superuser fields (raw
HTML and JS, the OnlyOffice and kkFileView servers) locked for admins who aren't superusers."""

import re

from django.test import SimpleTestCase

from apps.siteconf.serializers import GROUPS, form_fields

from .test_pages import TestBase


class ManagePageTests(TestBase):
    def test_the_sections_of_the_settings(self):
        self.client.force_login(self.boss)
        page = self.client.get("/manage/").content.decode()
        for key, label in (("site", "站点设置"), ("view", "显示设置"), ("links", "直链和短链"), ("proxy", "代理下载"),
                           ("security", "安全设置"), ("access", "访问控制"), ("diagnostics", "诊断信息")):
            self.assertIn(f'href="#{key}" data-section="{key}"', page)
            self.assertIn(f"<h2>{label}</h2>", page)
        self.assertEqual(len(re.findall(r'<script src="/static/siteconf/js/manage_settings\.js', page)), 1)  # shared, loaded once
        for group in ("site", "view", "link", "proxy", "security", "access"):
            self.assertIn(f'data-settings="{group}"', page)
        self.assertIn('name="proxy_ip_connections" inputmode="numeric" step="1" min="0" max="1000"', page)
        # Fields come from the serializers: names, labels, choices, limits.
        self.assertIn('<label for="set-site_name">站点名称</label>', page)
        self.assertIn('<option value="center">居中</option>', page)
        self.assertIn('name="max_show_size" inputmode="numeric" step="1" min="1" max="100000"', page)
        self.assertIn('data-reset="image_suffixes" data-default="gif, jpg, jpeg, png, bmp, webp, ico"', page)
        self.assertIn('data-thunder data-rules="*Thunder*', page)
        self.assertNotIn("disabled data-locked", page)  # a superuser changes everything

    def test_other_admins_see_the_raw_html_fields_locked(self):
        self.client.force_login(self.staff)
        page = self.client.get("/manage/").content.decode()
        for name in ("custom_css", "custom_js", "announcement", "guest_home_html"):
            self.assertRegex(page, rf'<textarea id="set-{name}" name="{name}"[^>]* disabled data-locked>')
        self.assertIn("只有超级管理员可以改这一项。", page)

    def test_other_admins_see_the_office_servers_locked(self):
        # D49. A locked field isn't sent (manage_settings.js skips data-locked), and the secret has no 清除.
        self.client.force_login(self.staff)
        page = self.client.get("/manage/").content.decode()
        for name in ("onlyoffice_url", "kkfileview_url"):
            self.assertRegex(page, rf'<input id="set-{name}" name="{name}"[^>]* disabled data-locked>')
        self.assertRegex(page, r'<input type="password" id="set-onlyoffice_secret" name="onlyoffice_secret"[^>]* disabled data-locked>')
        self.assertNotIn("data-secret-clear", page)
        self.assertIn('<textarea id="set-office_suffixes" name="office_suffixes" rows="2" spellcheck="false">', page)  # still theirs
        self.client.force_login(self.boss)
        page = self.client.get("/manage/").content.decode()
        self.assertRegex(page, r'<input type="password" id="set-onlyoffice_secret" name="onlyoffice_secret" autocomplete="new-password"[^>]*maxlength="1000">')
        self.assertIn("data-secret-clear hidden", page)


class LabelTests(SimpleTestCase):
    def test_names_that_start_lowercase_keep_it(self):
        # DRF capitalises a model field's name: 「kkFileView 打开方式」 came out as 「KkFileView 打开方式」.
        for group in GROUPS:
            for name, spec in form_fields(group).items():
                self.assertFalse(spec.label.startswith("Kk"), (group, name, spec.label))
        self.assertEqual(form_fields("view")["kkfileview_open_mode"].label, "kkFileView 打开方式")
