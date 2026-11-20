"""FakeIdpMixin: runs the fake provider (fake_idp.py) on 127.0.0.1 for a test class and makes a provider
that uses it; begin() and come_back() are the browser's two trips."""

from urllib.parse import parse_qs, urlsplit

from apps.sso.models import Provider

from .fake_idp import FakeIdp


class FakeIdpMixin:
    basic = False
    client_secret = "hfile-secret"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.idp = FakeIdp(client_secret=cls.client_secret, basic=cls.basic).serve()

    @classmethod
    def tearDownClass(cls):
        cls.idp.stop()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.idp.reset()
        self.provider = self.make_provider()

    def make_provider(self, **values):
        base = self.idp.base
        fields = {
            "code": "fake", "name": "测试服务商", "client_id": self.idp.client_id,
            "auth_url": f"{base}/authorize?prompt=consent&scope=old", "token_url": f"{base}/token",
            "userinfo_url": f"{base}/userinfo", "scope": "openid profile", "token_auth": "basic" if self.basic else "post",
            "id_field": "sub", "username_field": "preferred_username", "nickname_field": "name",
        }
        fields.update(values)
        provider = Provider(**fields)
        provider.set_secret(self.client_secret)
        provider.save()
        return provider

    def begin(self, client=None, *, code=None, next_path=None):
        """GET /sso/<code>/login: the provider's authorization address it sends the browser to."""
        client = client or self.client
        url = f"/sso/{code or self.provider.code}/login" + (f"?next={next_path}" if next_path else "")
        response = client.get(url)
        self.assertEqual(response.status_code, 303, response.content[:300])
        return response["Location"]

    def come_back(self, authorize_url, client=None, claims=None, **params):
        """The provider sends the browser back to the callback (with an approved code, or `params`)."""
        client = client or self.client
        if params:
            query = {key: values[-1] for key, values in parse_qs(urlsplit(authorize_url).query).items()}
            target = urlsplit(query["redirect_uri"]).path
            return client.get(target, params)
        back = self.idp.approve(authorize_url, claims)
        parts = urlsplit(back)
        return client.get(parts.path, {key: values[-1] for key, values in parse_qs(parts.query).items()})

    def login_via_sso(self, client=None, claims=None, next_path=None):
        return self.come_back(self.begin(client, next_path=next_path), client, claims)
