"""Confirmation des appartenances aux groupes de grades sur le Habbo français."""
from urllib.parse import quote

if __package__:
    from .habbo_http import ApiRequestError
else:
    from habbo_http import ApiRequestError


def groupes_publics_du_membre(hid, groupe, lire):
    """Seconde source après une omission dans la liste des membres du groupe."""
    chemin = "users/" + quote(hid, safe="")
    try:
        groupes = lire(chemin + "/groups")
        if groupes is None:
            return None
        if not isinstance(groupes, list) or not all(
            isinstance(g, dict) and isinstance(g.get("id"), str) and g["id"] for g in groupes
        ):
            raise ApiRequestError("response", endpoint=chemin + "/groups")
        ids = {g["id"] for g in groupes}
        if groupe not in ids:
            profil = lire(chemin)
            if (not isinstance(profil, dict) or profil.get("profileVisible") is not True
                    or profil.get("uniqueId") != hid):
                return None
        return ids
    except ApiRequestError as exc:
        if exc.status in (401, 403, 404):
            return None
        raise
