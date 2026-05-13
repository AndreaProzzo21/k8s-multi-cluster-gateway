"""
encryption.py
=============

Gestione della chiave di cifratura e helper per encrypt/decrypt.

Usa **Fernet** (dalla libreria ``cryptography``):
- AES-128-CBC con padding PKCS7
- HMAC-SHA256 per autenticazione del ciphertext (previene tampering)
- IV casuale incluso in ogni token → encrypt dello stesso valore produce
  token diversi ad ogni chiamata (non deterministico — sicuro)

La chiave viene letta da ``ENCRYPTION_KEY`` nell'environment.
Se mancante o malformata, il processo non parte (fail-fast).

Generare una nuova chiave (eseguire una sola volta):
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Poi aggiungere al .env:
    ENCRYPTION_KEY=<output del comando sopra>

IMPORTANTE: perdere la chiave significa perdere l'accesso a tutti i dati
cifrati nel database. Fare backup della chiave in un vault sicuro.
"""

import os

from cryptography.fernet import Fernet, InvalidToken


# ---------------------------------------------------------------------------
# Caricamento chiave (fail-fast all'avvio)
# ---------------------------------------------------------------------------

_raw_key = os.getenv("ENCRYPTION_KEY", "").strip()

if not _raw_key:
    raise RuntimeError(
        "ENCRYPTION_KEY non configurata. "
        "Generare una chiave con:\n"
        "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
        "e aggiungerla al file .env come ENCRYPTION_KEY=<valore>"
    )

try:
    _fernet = Fernet(_raw_key.encode())
except (ValueError, Exception) as exc:
    raise RuntimeError(
        f"ENCRYPTION_KEY non valida: {exc}\n"
        "Assicurarsi di usare una chiave generata con Fernet.generate_key()."
    ) from exc


# ---------------------------------------------------------------------------
# API pubblica
# ---------------------------------------------------------------------------

def encrypt(value: str) -> str:
    """
    Cifra una stringa e restituisce il token Fernet come stringa.

    Parameters
    ----------
    value : str
        Valore in chiaro da cifrare.

    Returns
    -------
    str
        Token Fernet base64-urlsafe, includente IV e HMAC.
        Sicuro da salvare in un campo VARCHAR/TEXT del database.

    Raises
    ------
    TypeError
        Se ``value`` non è una stringa.
    """
    if not isinstance(value, str):
        raise TypeError(f"encrypt() richiede una stringa, ricevuto {type(value)}")
    return _fernet.encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    """
    Decifra un token Fernet e restituisce il valore originale.

    Parameters
    ----------
    token : str
        Token Fernet prodotto da ``encrypt()``.

    Returns
    -------
    str
        Valore in chiaro originale.

    Raises
    ------
    ValueError
        Se il token è corrotto, manomesso, o cifrato con una chiave diversa.
    TypeError
        Se ``token`` non è una stringa.
    """
    if not isinstance(token, str):
        raise TypeError(f"decrypt() richiede una stringa, ricevuto {type(token)}")
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "Impossibile decifrare il valore: token corrotto, manomesso, "
            "o cifrato con una chiave diversa dall'attuale ENCRYPTION_KEY."
        ) from exc