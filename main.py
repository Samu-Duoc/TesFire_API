from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from bson import ObjectId
from pymongo.errors import PyMongoError

from database.mongodb import (
    client,
    cuentas_collection,
    transacciones_collection,
    ejecuciones_collection,
    logs_collection
)

app = FastAPI(title="API TestFire")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.(vercel\.app|devtunnels\.ms)|http://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cuentas autorizadas para el proyecto
CUENTAS_AUTORIZADAS = {
    "800000",
    "800002",
    "800003"
}
CUENTA_DASHBOARD = "800002"


# MODELOS DE DATOS
class Transaccion(BaseModel):
    account: str
    date: str
    transaction_type: str
    amount: str
    transaction_id: Optional[str] = None


class Registro(BaseModel):
    origen: str
    ejecucion: str
    total_transacciones: int = Field(ge=0)
    transacciones: List[Transaccion]


# FUNCIONES AUXILIARES
def serializar_documento(documento):
    documento["_id"] = str(documento["_id"])
    return documento


def registrar_log(
    nivel: str,
    evento: str,
    mensaje: str,
    ejecucion_id: Optional[str] = None,
    cuenta: Optional[str] = None,
    detalles: Optional[Dict[str, Any]] = None,
):
    documento = {
        "fecha_hora": datetime.now(timezone.utc),
        "ejecucion_id": ejecucion_id,
        "nivel": nivel,
        "evento": evento,
        "mensaje": mensaje,
        "cuenta": cuenta,
    }
    if detalles:
        documento["detalles"] = detalles
    try:
        logs_collection.insert_one(documento)
    except PyMongoError:
        # Los logs no deben impedir que el POST principal siga funcionando.
        pass


def clave_transaccion(transaccion: Transaccion, posicion: int) -> str:
    """Build a repeatable key; source transaction_id is preferred when supplied."""
    if transaccion.transaction_id:
        return f"source:{transaccion.transaction_id}"
    contenido = {
        "account": transaccion.account,
        "date": transaccion.date,
        "transaction_type": transaccion.transaction_type,
        "amount": transaccion.amount,
        "position": posicion,
    }
    serializado = json.dumps(contenido, sort_keys=True, separators=(",", ":"))
    return f"payload:{sha256(serializado.encode('utf-8')).hexdigest()}"


def parsear_monto(valor) -> float:
    if isinstance(valor, (int, float)):
        return abs(float(valor))
    texto = "".join(caracter for caracter in str(valor or "") if caracter.isdigit() or caracter in ",.-")
    texto = texto.replace(",", "")
    try:
        return abs(float(texto))
    except ValueError:
        return 0.0


def serializar_lista(registros):
    return [serializar_documento(registro) for registro in registros]


# INICIO DE LA API
@app.get("/")
def inicio():
    return {
        "success": True,
        "message": "API TestFire funcionando correctamente"
    }


# VERIFICAR CONEXION MONGODB
@app.get("/api/health")
def verificar_conexion():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError as error:
        raise HTTPException(
            status_code=503,
            detail=str(error)
        )


# CONSULTAR CUENTAS AUTORIZADAS
@app.get("/api/cuentas")
def consultar_cuentas():
    return {
        "success": True,
        "total": len(CUENTAS_AUTORIZADAS),
        "cuentas": sorted(CUENTAS_AUTORIZADAS)
    }


# RECIBIR Y GUARDAR TRANSACCIONES
@app.post("/api/transacciones")
def recibir_transacciones(datos: Registro):
    fecha_inicio = datetime.now(timezone.utc)
    ejecucion_id = None
    ejecucion_id_texto = None
    try:
        documento_ejecucion = {
            "origen": datos.origen,
            "ejecucion": datos.ejecucion,
            "fecha_inicio": fecha_inicio,
            "fecha_recepcion": fecha_inicio,
            "total_transacciones": len(datos.transacciones),
            "total_guardado": 0,
            "total_duplicados": 0,
            "estado": "procesando"
        }
        resultado_ejecucion = ejecuciones_collection.insert_one(documento_ejecucion)
        ejecucion_id = resultado_ejecucion.inserted_id
        ejecucion_id_texto = str(ejecucion_id)
        registrar_log(
            "INFO",
            "inicio_ejecucion",
            "Inicio de recepción de movimientos.",
            ejecucion_id_texto,
            datos.transacciones[0].account if datos.transacciones else None,
            {"origen": datos.origen, "ejecucion": datos.ejecucion},
        )

        # Validar cuentas recibidas
        for transaccion in datos.transacciones:

            if transaccion.account not in CUENTAS_AUTORIZADAS:
                raise HTTPException(
                    status_code=403,
                    detail=f"Cuenta no autorizada: {transaccion.account}"
                )

            if transaccion.transaction_type not in [
                "Deposit",
                "Withdrawal"
            ]:
                raise HTTPException(
                    status_code=422,
                    detail="Tipo de transacción no válido"
                )

        fecha_recepcion = datetime.now(timezone.utc)
        registrar_log("INFO", "consulta_movimientos", "Movimientos recibidos desde Rocketbot.", ejecucion_id_texto, detalles={"total": len(datos.transacciones)})

        # Preparar transacciones individuales
        documentos = []

        for posicion, transaccion in enumerate(datos.transacciones):

            documento = transaccion.model_dump()

            documento["transaction_key"] = clave_transaccion(transaccion, posicion)
            documento["ejecucion_id"] = ejecucion_id_texto
            documento["fecha_recepcion"] = fecha_recepcion
            documento["origen"] = datos.origen

            # Normalizar tipo de movimiento
            if documento["transaction_type"] == "Deposit":
                documento["movement_type"] = "Credit"
            else:
                documento["movement_type"] = "Debit"

            documento.pop("transaction_id", None)
            documentos.append(documento)

        # Guardar transacciones
        total_guardado = 0
        total_duplicados = 0
        for documento in documentos:
            if transacciones_collection.find_one({"transaction_key": documento["transaction_key"]}, {"_id": 1}):
                total_duplicados += 1
                continue
            transacciones_collection.insert_one(documento)
            total_guardado += 1
        registrar_log("INFO", "almacenamiento_mongodb", "Movimientos procesados en MongoDB.", ejecucion_id_texto, detalles={"guardados": total_guardado, "duplicados": total_duplicados})

        # Actualizar registro de ejecución
        ejecuciones_collection.update_one(
            {"_id": ejecucion_id},
            {
                "$set": {
                    "total_guardado": total_guardado,
                    "total_duplicados": total_duplicados,
                    "fecha_fin": datetime.now(timezone.utc),
                    "estado": "procesada"
                }
            }
        )
        registrar_log("INFO", "finalizacion_exitosa", "Ejecución finalizada correctamente.", ejecucion_id_texto, detalles={"guardados": total_guardado})

        return {
            "success": True,
            "message": "Transacciones almacenadas correctamente",
            "ejecucion_id": str(ejecucion_id),
            "total": total_guardado,
            "total_duplicados": total_duplicados
        }

    except HTTPException as error:
        if ejecucion_id:
            try:
                ejecuciones_collection.update_one(
                    {"_id": ejecucion_id},
                    {"$set": {"estado": "fallida", "fecha_fin": datetime.now(timezone.utc), "mensaje_error": str(error.detail)}},
                )
            except PyMongoError:
                pass
        registrar_log("ERROR", "error_validacion", str(error.detail), ejecucion_id_texto, datos.transacciones[0].account if datos.transacciones else None)
        raise

    except PyMongoError as error:
        if ejecucion_id:
            try:
                ejecuciones_collection.update_one(
                    {"_id": ejecucion_id},
                    {"$set": {"estado": "fallida", "fecha_fin": datetime.now(timezone.utc), "mensaje_error": "Error al guardar los datos en MongoDB"}},
                )
            except PyMongoError:
                pass
        registrar_log("ERROR", "error_mongodb", "Error al guardar los datos en MongoDB.", ejecucion_id_texto, detalles={"error": str(error)})
        raise HTTPException(
            status_code=500,
            detail="Error al guardar los datos en MongoDB"
        )


# CONSULTAR TRANSACCIONES
@app.get("/api/transacciones")
def consultar_transacciones(
    account: str = None,
    movement_type: str = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    ejecucion_id: Optional[str] = None,
):

    filtros = {}

    if account:
        if account not in CUENTAS_AUTORIZADAS:
            raise HTTPException(
                status_code=403,
                detail="Cuenta no autorizada"
            )

        filtros["account"] = account

    if movement_type:
        if movement_type not in ["Credit", "Debit"]:
            raise HTTPException(
                status_code=422,
                detail="Filtro de movimiento no válido"
            )

        filtros["movement_type"] = movement_type
    if fecha_desde or fecha_hasta:
        filtros["date"] = {}
        if fecha_desde:
            filtros["date"]["$gte"] = fecha_desde
        if fecha_hasta:
            filtros["date"]["$lte"] = fecha_hasta
    if ejecucion_id:
        filtros["ejecucion_id"] = ejecucion_id

    try:
        registros = list(
            transacciones_collection.find(filtros).sort(
                "fecha_recepcion", -1
            )
        )

        registros = serializar_lista(registros)

        return {
            "success": True,
            "total_registros": len(registros),
            "datos": registros
        }

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Error al consultar las transacciones"
        )


# CONSULTAR EJECUCIONES
@app.get("/api/ejecuciones")
def consultar_ejecuciones(
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    estado: Optional[str] = None,
):

    try:
        filtros = {}
        campo_fecha = "fecha_recepcion"
        if fecha_desde or fecha_hasta:
            filtros[campo_fecha] = {}
            if fecha_desde:
                filtros[campo_fecha]["$gte"] = datetime.fromisoformat(fecha_desde)
            if fecha_hasta:
                filtros[campo_fecha]["$lte"] = datetime.fromisoformat(fecha_hasta)
        if estado:
            filtros["estado"] = estado
        registros = list(
            ejecuciones_collection.find(filtros).sort(
                "fecha_recepcion", -1
            )
        )

        registros = serializar_lista(registros)

        return {
            "success": True,
            "total_ejecuciones": len(registros),
            "datos": registros
        }

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Error al consultar ejecuciones"
        )


@app.get("/api/logs")
def consultar_logs(
    nivel: Optional[str] = None,
    ejecucion_id: Optional[str] = None,
    account: Optional[str] = None,
    limite: int = Query(default=200, ge=1, le=1000),
):
    filtros = {}
    if nivel:
        if nivel not in ["INFO", "WARNING", "ERROR"]:
            raise HTTPException(status_code=422, detail="Nivel de log no válido")
        filtros["nivel"] = nivel
    if ejecucion_id:
        filtros["ejecucion_id"] = ejecucion_id
    if account:
        filtros["cuenta"] = account
    try:
        registros = list(logs_collection.find(filtros).sort("fecha_hora", -1).limit(limite))
        return {"success": True, "total_logs": len(registros), "datos": serializar_lista(registros)}
    except PyMongoError:
        raise HTTPException(status_code=500, detail="Error al consultar logs")


@app.get("/api/estadisticas")
def consultar_estadisticas(account: str = CUENTA_DASHBOARD):
    if account not in CUENTAS_AUTORIZADAS:
        raise HTTPException(status_code=403, detail="Cuenta no autorizada")
    try:
        transacciones = list(transacciones_collection.find({"account": account}, {"movement_type": 1, "amount": 1, "date": 1, "ejecucion_id": 1}))
        tipos = {}
        fechas = {}
        ejecuciones_por_transaccion = {}
        for transaccion in transacciones:
            movimiento = transaccion.get("movement_type", "Sin tipo")
            monto = parsear_monto(transaccion.get("amount"))
            tipos.setdefault(movimiento, {"_id": movimiento, "cantidad": 0, "monto": 0.0})
            tipos[movimiento]["cantidad"] += 1
            tipos[movimiento]["monto"] += monto
            fecha = transaccion.get("date", "Sin fecha")
            fechas.setdefault(fecha, {"fecha": fecha, "cantidad": 0, "monto": 0.0})
            fechas[fecha]["cantidad"] += 1
            fechas[fecha]["monto"] += monto
            ejecucion = transaccion.get("ejecucion_id", "Sin ejecución")
            ejecuciones_por_transaccion[ejecucion] = ejecuciones_por_transaccion.get(ejecucion, 0) + 1
        por_tipo = list(tipos.values())
        por_fecha = sorted(fechas.values(), key=lambda item: item["fecha"])
        por_ejecucion = [{"_id": ejecucion, "cantidad": cantidad} for ejecucion, cantidad in ejecuciones_por_transaccion.items()]
        por_ejecucion.sort(key=lambda item: item["cantidad"], reverse=True)
        ejecuciones = list(ejecuciones_collection.aggregate([
            {"$group": {"_id": "$estado", "cantidad": {"$sum": 1}}},
        ]))
        return {"success": True, "cuenta": account, "por_tipo": por_tipo, "por_fecha": por_fecha, "por_ejecucion": por_ejecucion, "ejecuciones_por_estado": ejecuciones}
    except PyMongoError:
        raise HTTPException(status_code=500, detail="Error al calcular estadísticas históricas")


@app.get("/api/ejecuciones/{ejecucion_id}/transacciones")
def consultar_transacciones_ejecucion(ejecucion_id: str):
    try:
        registros = list(transacciones_collection.find({"ejecucion_id": ejecucion_id}).sort("fecha_recepcion", -1))
        return {"success": True, "total_registros": len(registros), "datos": serializar_lista(registros)}
    except PyMongoError:
        raise HTTPException(status_code=500, detail="Error al consultar transacciones de la ejecución")


# CONSULTAR UN REGISTRO ESPECIFICO
@app.get("/api/transacciones/{id}")
def consultar_transaccion(id: str):

    if not ObjectId.is_valid(id):
        raise HTTPException(
            status_code=400,
            detail="ID de transacción inválido"
        )

    try:
        registro = transacciones_collection.find_one(
            {"_id": ObjectId(id)}
        )

        if registro is None:
            raise HTTPException(
                status_code=404,
                detail="Registro no encontrado"
            )

        return {
            "success": True,
            "datos": serializar_documento(registro)
        }

    except HTTPException:
        raise

    except PyMongoError:
        raise HTTPException(
            status_code=500,
            detail="Error al consultar el registro"
        )
