"""应用入口：参数解析、依赖组装与HTTP服务生命周期。"""
import argparse
from pathlib import Path
from types import SimpleNamespace

from src.audit import AuditRecorder
from src.http_api import create_server
from src.netting_repository import NettingRepository
from src.netting_rules import NettingRules
from src.netting_service import NettingService
from src.repository import Repository
from src.rules import DomainRules
from src.service import Service


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "securities-settlement.db"
DEFAULT_PORT = 8324


def build_services(db_path: str):
    """组装单据与净额批次两套服务，共享同一SQLite数据库。"""
    repository = Repository(db_path)
    audit = AuditRecorder(repository)
    netting = NettingService(repository, NettingRepository(db_path), NettingRules())
    records = Service(repository, DomainRules(), audit, record_guards=[netting.record_lock_guard])
    return SimpleNamespace(records=records, netting=netting)


def build_service(db_path: str) -> Service:
    return build_services(db_path).records


def parse_args():
    parser = argparse.ArgumentParser(description="证券结算与企业行动处理")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite数据库路径")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP监听端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    Path(args.db).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    services = build_services(args.db)
    server = create_server(args.host, args.port, services.records, BASE_DIR / "static", netting_service=services.netting)
    print("证券结算与企业行动处理 listening on http://%s:%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
