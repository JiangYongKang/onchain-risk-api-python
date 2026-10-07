def test_app_imports():
    from app.main import app  # noqa: F401

    routes = {r.path for r in app.routes}
    assert "/transactions/batches" in routes
    assert "/rules/publish" in routes
    assert "/rules/rollback" in routes
    assert "/rules/dry-run" in routes
    assert "/risk" in routes
