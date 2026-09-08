import pytest
from datetime import datetime, timedelta, timezone
from app.services.monitor import (
    get_live_metrics,
    get_top_processes,
    record_metric_snapshot,
    get_history,
)
from app.models import ServerMetric

def test_get_live_metrics():
    metrics = get_live_metrics()
    assert "cpu" in metrics
    assert "percent" in metrics["cpu"]
    assert "memory" in metrics
    assert "percent" in metrics["memory"]
    assert "disk" in metrics
    assert "percent" in metrics["disk"]
    assert "load" in metrics
    assert "network" in metrics
    assert "uptime_seconds" in metrics

def test_get_top_processes():
    procs = get_top_processes(limit=10, sort_by="cpu")
    assert isinstance(procs, list)
    if procs:
        p = procs[0]
        assert "pid" in p
        assert "name" in p
        assert "cpu_percent" in p
        assert "memory_percent" in p

def test_record_and_get_history(db):
    # Record a snapshot
    record_metric_snapshot(db)
    
    # Check history returns at least 1 record
    history = get_history(db, hours=1)
    assert len(history) >= 1
    record = history[-1]
    assert "time" in record
    assert "cpu" in record
    assert "memory" in record

def test_history_pruning(db):
    # Insert old record
    old_time = datetime.now(timezone.utc) - timedelta(days=8)
    old_metric = ServerMetric(
        created_at=old_time,
        cpu_percent=10.0,
        memory_percent=20.0,
        memory_used_mb=1000,
        swap_percent=0.0,
        disk_percent=30.0,
        load_1m=0.5,
        load_5m=0.5,
        load_15m=0.5,
        net_rx_kb=0,
        net_tx_kb=0,
    )
    db.add(old_metric)
    db.commit()
    
    # Record snapshot, which triggers pruning of entries > 7 days old
    record_metric_snapshot(db)
    
    remaining = db.query(ServerMetric).filter(ServerMetric.created_at == old_time).first()
    assert remaining is None
