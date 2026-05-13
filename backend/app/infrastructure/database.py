"""
database.py
===========

Modelli SQLAlchemy e configurazione della sessione del gateway.

Cifratura dei campi sensibili
------------------------------
I campi ``gateway_password``, ``k8s_token`` e ``ca_cert`` usano
``EncryptedString``, un ``TypeDecorator`` che cifra automaticamente
i valori prima della persistenza e li decifra dopo il recupero.

Il resto del codice (registry.py, auth_handler.py, admin_routes.py)
non sa che i dati sono cifrati su disco: legge e scrive stringhe normali.

Campi cifrati
-------------
- ``ProfileModel.gateway_password``  — password di login al gateway
- ``ProfileModel.k8s_token``         — SA token Kubernetes
- ``ClusterModel.ca_cert``           — CA Certificate PEM

Campi in chiaro
---------------
- ``ClusterModel.id``, ``ClusterModel.name``, ``ClusterModel.host``
- ``ProfileModel.cluster_id``, ``ProfileModel.name``
  (usati come chiavi di lookup — devono restare leggibili)
"""

import os

from sqlalchemy import Column, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.infrastructure.encryption import decrypt, encrypt


# ---------------------------------------------------------------------------
# Connessione al database
# ---------------------------------------------------------------------------
# DB_PATH è il path del file SQLite (es. "data/gateway.db" o un path assoluto).
# create_engine aggiunge il prefisso sqlite:/// davanti, esattamente come
# nell'implementazione originale.

DB_PATH = os.getenv("DATABASE_URL", "sqlite:///data/gateway.db")
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ---------------------------------------------------------------------------
# EncryptedString — TypeDecorator per la cifratura automatica
# ---------------------------------------------------------------------------

class EncryptedString(TypeDecorator):
    """
    Tipo SQLAlchemy che cifra i valori prima di INSERT/UPDATE
    e li decifra dopo SELECT.

    È un drop-in replacement di ``String`` / ``Text``: il codice
    che accede ai modelli riceve sempre la stringa in chiaro.

    Valori NULL
    -----------
    Se il valore è ``None``, viene passato invariato — SQLAlchemy
    gestisce NULL nativamente e non ha senso cifrare NULL.

    Migrazione da dati in chiaro
    ----------------------------
    Se nel DB esistono righe con valori non ancora cifrati, eseguire
    ``migrate_encrypt.py`` PRIMA di riavviare il gateway con questa versione.
    """

    impl     = Text   # tipo colonna sul DDL SQLite
    cache_ok = True   # indica a SQLAlchemy che il tipo è deterministico

    def process_bind_param(self, value, dialect):
        """Cifra prima di scrivere sul DB (INSERT / UPDATE)."""
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value, dialect):
        """Decifra dopo aver letto dal DB (SELECT)."""
        if value is None:
            return None
        return decrypt(value)


# ---------------------------------------------------------------------------
# Modelli
# ---------------------------------------------------------------------------

class ClusterModel(Base):
    __tablename__ = "clusters"

    id      = Column(String,          primary_key=True)   # es. "TESI"
    name    = Column(String,          nullable=False)
    host    = Column(String,          nullable=False)      # URL API server
    ca_cert = Column(EncryptedString, nullable=True)       # CA cert PEM — cifrato

    profiles = relationship(
        "ProfileModel",
        back_populates="cluster",
        cascade="all, delete",
    )


class ProfileModel(Base):
    __tablename__ = "profiles"

    id               = Column(Integer,          primary_key=True, autoincrement=True)
    cluster_id       = Column(String,           ForeignKey("clusters.id"))
    name             = Column(String,           nullable=False)        # es. "admin", "dev"
    gateway_password = Column(EncryptedString,  nullable=False)        # cifrato
    k8s_token        = Column(EncryptedString,  nullable=False)        # cifrato

    cluster = relationship("ClusterModel", back_populates="profiles")


# ---------------------------------------------------------------------------
# Creazione tabelle
# ---------------------------------------------------------------------------

def init_db():
    """Crea le tabelle se non esistono. Chiamata all'avvio del gateway."""
    if not os.path.exists("data"):
        os.makedirs("data")
    Base.metadata.create_all(bind=engine)