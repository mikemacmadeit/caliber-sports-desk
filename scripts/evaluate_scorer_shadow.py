"""Read-only evaluation of frozen pregame scorer comparisons."""
import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from spe.models.player_td.scorer_shadow_record import record


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--database',type=Path,default=Path('data/caliber.db'))
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    path=args.database.resolve().as_posix()
    engine=create_engine(f'sqlite:///file:{path}?mode=ro&uri=true')
    with Session(engine) as db:result=record(db)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))
