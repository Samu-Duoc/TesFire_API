import os
from pymongo import MongoClient
from dotenv import load_dotenv

# Cargar variables del archivo .env
load_dotenv()

# Obtener URI de MongoDB desde el archivo .env
MONGODB_URI = os.getenv("MONGODB_URI")

if not MONGODB_URI:
    raise RuntimeError("No se encontró la variable MONGODB_URI")

# Crear conexión
client = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    maxPoolSize=5,
)

# Seleccionar base de datos
db = client["testfire"]

# Colecciones
cuentas_collection = db["cuentas"]
transacciones_collection = db["transacciones"]
ejecuciones_collection = db["ejecuciones"]
logs_collection = db["logs"]


def verificar_conexion():
    try:
        client.admin.command("ping")
        return True
    except Exception as error:
        print(f"Error de conexión MongoDB: {error}")
        return False
