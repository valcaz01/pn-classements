"""Groupes Habbo officiels, du premier badge au grade le plus élevé."""
GROUPE_CADET = "g-hhfr-3dcf0cad12934ba41c258628d82bde40"
GROUPE_PA = "g-hhfr-98146efd0f202d369c38a5d5265d124e"
GROUPE_GDP = "g-hhfr-f9e03b6a56b74caf069c4b3a3d80aabd"
GROUPES_GRADES = (
    "g-hhfr-e8723fe59e066f9847347ab936a0e9c6",  # Gradé débutant
    "g-hhfr-f1c6e473ac8a29547c44a4675b750585",  # Gradé intermédiaire
    "g-hhfr-d7080b2c1318a56acc06cf8b2b7568be",  # Gradé confirmé
    "g-hhfr-c52059ba6f45cc055c9fb5b9b5a3bc64",  # Haut gradé
    "g-hhfr-c2e7127da613e13151bd947c7593bb95",  # Superviseur
    "g-hhfr-918ddc2eee80c1dc557c1e4d4e023828",  # Direction
)
GROUPES_SUPERIEURS = (GROUPE_PA, GROUPE_GDP, *GROUPES_GRADES)
EXCLUS_FORMATIONS = frozenset({"hhfr-e8d398726baabd48f5a494d57483ff23"})
