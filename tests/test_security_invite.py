import unittest
from security_invite import eligible,issue_invitation

STAFF="223456789012345678"
ADMIN="323456789012345678"
OWNER="423456789012345678"
ACTOR="523456789012345678"
TARGET="623456789012345678"
GUILD="123456789012345678"
LINK="https://security.example.test/staff-security/#invite="+("A"*43)
KEY="test-only-private-hobby-bot-enrollment-key-123456"

class Response:
    def __init__(self,status_code=201,link=LINK):
        self.status_code=status_code
        self.link=link
    def json(self):
        return {"link":self.link}

class EnrollmentTests(unittest.TestCase):
    def test_exact_role_ids(self):
        opts={"staff_role":STAFF,"admin_role":ADMIN,"owner_role":OWNER}
        self.assertTrue(eligible([ADMIN],[STAFF],**opts))
        self.assertTrue(eligible([OWNER],[ADMIN],**opts))
        self.assertFalse(eligible([STAFF],[STAFF],**opts))
        self.assertFalse(eligible(["999999999999999999"],[STAFF],**opts))
        self.assertFalse(eligible([OWNER],["999999999999999999"],**opts))
        self.assertFalse(eligible([OWNER],[STAFF],**{**opts,"owner_role":""}))

    def test_bot_only_requests_private_links_with_exact_backend(self):
        seen=[]
        def fake(url,**kwargs):
            seen.append((url,kwargs))
            return Response()
        result=issue_invitation(post=fake,api_url="https://private.example.test",
            public_origin="https://security.example.test",bot_key=KEY,
            guild_id=GUILD,actor_id=ACTOR,target_id=TARGET,actor_roles=[ADMIN])
        self.assertEqual(result,LINK)
        self.assertEqual(seen[0][0],"https://private.example.test/staff-enrollment-invites/bot/issue")
        self.assertEqual(seen[0][1]["headers"]["x-hc-invitation-bot-key"],KEY)
        self.assertNotIn("pin",seen[0][1]["json"])
        self.assertNotIn("secret",seen[0][1]["json"])
        self.assertEqual(seen[0][1]["timeout"],10)

    def test_reject_bad_link_and_backend_errors(self):
        opts=dict(api_url="https://private.example.test",
                  public_origin="https://security.example.test",bot_key=KEY,
                  guild_id=GUILD,actor_id=ACTOR,target_id=TARGET,actor_roles=[ADMIN])
        for link in ("https://evil.example/#invite="+("A"*43),
                     "https://security.example.test/?token="+("A"*43),
                     "https://security.example.test/staff-security/#invite=bad"):
            with self.subTest(link=link),self.assertRaises(ValueError):
                issue_invitation(post=lambda *args,**kwargs:Response(link=link),**opts)
        with self.assertRaises(ValueError):
            issue_invitation(post=lambda *args,**kwargs:Response(status_code=403),**opts)
        with self.assertRaises(ValueError):
            issue_invitation(post=lambda *args,**kwargs:Response(),
                             **{**opts,"bot_key":"bad"})
        with self.assertRaises(ValueError):
            issue_invitation(post=lambda *args,**kwargs:Response(),
                             **{**opts,"api_url":"http://untrusted.example"})
if __name__=="__main__":
    unittest.main()
