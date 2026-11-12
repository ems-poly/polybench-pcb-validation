"""TEST HELPER, for tests only: do not copy this into real use. Operator session for the end-to-end scripts (D10). The app only accepts operator POSTs that carry its pb_op cookie, which GET / hands to a
browser. The scripts start their own server, so they give it a fixed token through the environment (inherited by the child process; the app warns loudly and refuses port 8750) and
send the same cookie from urllib. Import this module before the server is started. Playwright pages get the cookie from GET / as usual."""
import os
import secrets
import urllib.request

TOKEN = os.environ.setdefault("POLYBENCH_OP_TOKEN", secrets.token_urlsafe(24))
_opener = urllib.request.build_opener()
_opener.addheaders = [("Cookie", "pb_op=" + TOKEN)]
urllib.request.install_opener(_opener)
