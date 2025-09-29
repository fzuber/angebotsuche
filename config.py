"""
Gemeinsame Konfiguration für TED- und BKMS-Adapter
"""

KEYWORDS = [
    # deutsch
    "Sozialforschung","Marktforschung","Bevölkerungsbefragung","Meinungsforschung",
    "quantitative Befragung","Politikforschung","Evidenz","Medienforschung",
    "quantitative Forschung","Forschungsinstitut","Medienmessung","Imageforschung",
    "Imagemessung","B2B","b2b","Umfrage","Befragung","Panel","Studie",
    # englisch
    "social research","market research","population survey","opinion research",
    "public opinion","opinion poll","quantitative survey","media research",
    "audience measurement","media measurement","image research","brand tracking",
    "questionnaire","sample","panel study","evaluation study","impact evaluation",
]

CPV_WHITELIST = [
    "79300000","79310000","79311000","79311100","79311200","79311300",
    "79311400","79315000","79320000","79330000"
]

# daraus abgeleitete Sets/Präfixe
CPV_SET = set(CPV_WHITELIST)
CPV_PREFIXES = tuple(sorted({c[:4] for c in CPV_WHITELIST}))
