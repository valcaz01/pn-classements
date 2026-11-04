"""Lectures JSON avec reprise bornée ; aucune écriture n'est rejouée."""
from __future__ import annotations

import http.client
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


LOG = logging.getLogger("pn.http")
RETRY_DELAYS = (2, 5)
FAILURE_MARKER = "PN_API_FAILURE:"


class ApiRequestError(RuntimeError):
    """Cause structurée, sans clé ni contenu HTML provenant du service distant."""
    def __init__(self, kind, service="Habbo", status=None, endpoint=""):
        self.kind, self.service, self.status = kind, service, status
        detail = {
            "timeout": "délai de réponse dépassé",
            "network": "connexion interrompue ou indisponible",
            "unavailable": "service temporairement indisponible",
            "rate_limit": "limite de requêtes atteinte",
            "access": "accès refusé",
            "response": "réponse JSON incomplète ou invalide",
            "http": "requête refusée",
        }.get(kind, "requête impossible")
        code = f" (HTTP {status})" if status else ""
        super().__init__(f"{service} : {detail}{code}" + (f" sur {endpoint}" if endpoint else ""))

    def summary(self):
        return {"kind": self.kind, "service": self.service, "status": self.status}


def retry_after(headers, now=None):
    """Retry-After accepte un nombre de secondes ou une date HTTP."""
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            return max(0, (date - (now or datetime.now(timezone.utc))).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def request_json(request, *, service="Habbo", opener=None, sleep=None):
    """Trois tentatives GET au maximum, délai socket 20 s, pauses 2 puis 5 s.

    Les POST/PATCH/PUT ne sont jamais rejoués : une réponse perdue ne prouve
    pas que l'écriture a échoué. Un long Retry-After arrête cette lecture au
    lieu de bloquer le bot ou de réessayer avant la date autorisée.
    """
    opener = opener or urllib.request.urlopen
    sleep = sleep or time.sleep
    read_only = request.get_method() == "GET" and request.data is None
    attempts = len(RETRY_DELAYS) + 1 if read_only else 1
    parsed = urllib.parse.urlsplit(request.full_url)
    # Les URL de webhooks Discord contiennent un secret dans le chemin.
    endpoint = parsed.path if service == "Habbo" else parsed.hostname or ""
    for attempt in range(attempts):
        delay_hint = None
        try:
            with opener(request, timeout=20) as response:
                body = response.read()
                if not body and not read_only:
                    return {}  # notamment une écriture réussie avec réponse HTTP 204
                result = json.loads(body)
                if result is None and read_only:
                    raise json.JSONDecodeError("Réponse de lecture absente", "null", 0)
                return result
        except urllib.error.HTTPError as exc:
            status = exc.code
            delay_hint = retry_after(exc.headers)
            exc.close()
            if status == 404:
                return None
            kind = ("rate_limit" if status == 429 else "unavailable" if status in {500, 502, 503, 504}
                    else "access" if status in {401, 403} else "http")
            error = ApiRequestError(kind, service, status, endpoint)
            retry = kind in {"rate_limit", "unavailable"}
        except ssl.SSLCertVerificationError:
            error = ApiRequestError("access", service, endpoint=endpoint)
            retry = False
        except urllib.error.URLError as exc:
            kind = "timeout" if isinstance(exc.reason, TimeoutError) or "timed out" in str(exc.reason).lower() else "network"
            error = ApiRequestError(kind, service, endpoint=endpoint)
            retry = not isinstance(exc.reason, ssl.SSLCertVerificationError)
        except TimeoutError:
            error = ApiRequestError("timeout", service, endpoint=endpoint)
            retry = True
        except (ConnectionError, http.client.HTTPException):
            error = ApiRequestError("network", service, endpoint=endpoint)
            retry = True
        except (json.JSONDecodeError, UnicodeDecodeError):
            error = ApiRequestError("response", service, endpoint=endpoint)
            retry = True
        if not retry or attempt == attempts - 1 or (delay_hint is not None and delay_hint > 30):
            raise error from None
        delay = max(RETRY_DELAYS[attempt], delay_hint or 0)
        LOG.warning("%s ; lecture %s/3 dans %ss", error, attempt + 2, delay)
        sleep(delay)
