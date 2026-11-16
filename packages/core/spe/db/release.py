"""Argument-free migration entry point for container pre-deploy commands."""
from pathlib import Path
from alembic import command
from alembic.config import Config


def main():
    root = Path(__file__).resolve().parents[4]
    config = Config(str(root / 'infra/alembic.ini'))
    config.set_main_option('script_location', str(root / 'infra/alembic'))
    command.upgrade(config, 'head')


if __name__ == '__main__':
    main()
