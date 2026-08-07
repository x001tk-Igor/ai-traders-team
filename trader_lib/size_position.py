import math


def compute_lots(*, risk_usd, entry, sl, symbol_info):
    """Размер лота так, чтобы риск при срабатывании SL ≈ risk_usd, но НЕ БОЛЬШЕ.
    Если бюджет не покрывает даже минимальный лот → 0.0 (сделки нет): округлять
    вверх к min нельзя, иначе пробьём выданный риск-бюджет."""
    point = symbol_info["point"]
    contract = symbol_info["trade_contract_size"]
    step = symbol_info["volume_step"]
    vmin = symbol_info["volume_min"]
    vmax = symbol_info["volume_max"]
    sl_points = abs(entry - sl) / point
    if sl_points <= 0:
        return 0.0
    value_per_point_per_lot = contract * point  # прибл. для инстр., котируемых в валюте счёта
    loss_per_lot = sl_points * value_per_point_per_lot
    if loss_per_lot <= 0:
        return 0.0
    raw = risk_usd / loss_per_lot
    if raw < vmin:
        return 0.0
    lots = math.floor(raw / step) * step
    lots = min(lots, vmax)
    return round(lots, 8)
