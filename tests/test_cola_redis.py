class MockAsyncRedis:
    """Emulador en memoria del cliente asíncrono de Redis para pruebas unitarias."""

    def __init__(self):
        self.counters = {}
        self.keys_store = {}
        self.sets = {}
        self.zsets = {}

    def pipeline(self, transaction=True):
        return MockPipeline(self)

    async def ping(self):
        return True
    
    async def eval(self, script: str, numkeys: int, *keys_and_args):
        """Emula la ejecución de scripts Lua de Redis para pruebas unitarias."""
        import json

        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]

        # 🟢 1. Emulación de ENQUEUE_LUA_SCRIPT
        if "counter_key" in script or "pendientes_count" in script or "INCR" in script:
            smart_code = args[0]
            tipo_operacion = args[1]
            payload_json_raw = args[2]
            error_inicial = args[3]
            max_intentos = int(args[4])
            now_iso = args[5]
            proximo_reintento_iso = args[6]
            now_ts = float(args[7])
            proximo_reintento_ts = float(args[8])
            correlation_id = args[9]
            estado_pendiente = args[10] if len(args) > 10 else "PENDIENTE"

            index_key = keys[0]
            pending_set_key = keys[1]
            pending_zset_key = keys[2]
            created_zset_key = keys[3]
            counter_key = keys[4]

            existing_id = await self.get(index_key)
            pendientes_count = await self.scard(pending_set_key)

            if existing_id and await self.sismember(pending_set_key, existing_id):
                item_key = f"{{sfc:queue}}:item:{existing_id}"
                raw_item = await self.get(item_key)
                if raw_item:
                    data = json.loads(raw_item)
                    data["payload_json"] = json.loads(payload_json_raw)
                    data["ultimo_error"] = error_inicial
                    data["updated_at"] = now_iso
                    data["proximo_reintento_at"] = proximo_reintento_iso
                    data["correlation_id"] = correlation_id
                    data["es_duplicado"] = True

                    await self.set(item_key, json.dumps(data, ensure_ascii=False))
                    await self.zadd(pending_zset_key, {str(existing_id): proximo_reintento_ts})

                    return json.dumps({
                        "is_new": False,
                        "data": data,
                        "pendientes_previos": pendientes_count
                    })

            item_id = await self.incr(counter_key)
            item_key = f"{{sfc:queue}}:item:{item_id}"

            item_data = {
                "id": int(item_id),
                "smart_code": smart_code,
                "tipo_operacion": tipo_operacion,
                "payload_json": json.loads(payload_json_raw),
                "estado": estado_pendiente,
                "sfc_completado": False,
                "sfc_response": None,
                "intentos": 1,
                "max_intentos": max_intentos,
                "ultimo_error": error_inicial,
                "proximo_reintento_at": proximo_reintento_iso,
                "created_at": now_iso,
                "updated_at": now_iso,
                "correlation_id": correlation_id,
                "es_duplicado": False
            }

            await self.set(item_key, json.dumps(item_data, ensure_ascii=False))
            await self.sadd(pending_set_key, str(item_id))
            await self.zadd(pending_zset_key, {str(item_id): proximo_reintento_ts})
            await self.zadd(created_zset_key, {str(item_id): now_ts})
            await self.set(index_key, str(item_id))

            return json.dumps({
                "is_new": True,
                "data": item_data,
                "pendientes_previos": pendientes_count
            })

        # 🟢 2. Emulación de MARK_SUCCESS_LUA_SCRIPT
        if "estado_completed" in script or "completed_set_key" in script:
            item_key = keys[0]
            pending_set_key = keys[1]
            completed_set_key = keys[2]
            pending_zset_key = keys[3]
            claim_key = keys[4]

            item_id = args[0]
            now_iso = args[1]
            estado_completed = args[2]

            raw_item = await self.get(item_key)
            if not raw_item:
                return json.dumps({"success": False, "reason": "item_not_found"})

            data = json.loads(raw_item)
            data["estado"] = estado_completed
            data["updated_at"] = now_iso

            await self.set(item_key, json.dumps(data, ensure_ascii=False))
            await self.srem(pending_set_key, item_id)
            await self.sadd(completed_set_key, item_id)
            await self.zrem(pending_zset_key, item_id)
            await self.delete(claim_key)

            if data.get("smart_code"):
                index_key = f"{{sfc:queue}}:index:{data['smart_code']}"
                await self.delete(index_key)

            return json.dumps({"success": True})

        # 🟢 3. Emulación de CLAIM_ITEM_LUA_SCRIPT
        if "already_claimed" in script or "claimed = false" in script:
            pending_set_key = keys[0]
            claim_key = keys[1]

            item_id = args[0]
            worker_id = args[1]
            lease_px = float(args[2])

            is_pending = await self.sismember(pending_set_key, item_id)
            if not is_pending:
                return json.dumps({"claimed": False, "reason": "not_pending"})

            res_set = await self.set(claim_key, worker_id, nx=True, px=lease_px)
            if not res_set:
                return json.dumps({"claimed": False, "reason": "already_claimed"})

            return json.dumps({"claimed": True})

        # 🟢 4. Emulación de EXTEND_LEASE_LUA_SCRIPT
        if "owner_mismatch_or_expired" in script:
            claim_key = keys[0]
            worker_id = args[0]
            lease_px = float(args[1])

            current_owner = await self.get(claim_key)
            if current_owner == worker_id:
                return json.dumps({"extended": True})
            return json.dumps({"extended": False, "reason": "owner_mismatch_or_expired"})

        return json.dumps({})