"""add Pokemon battle history and Lycanroc form metadata"""
from alembic import op
import sqlalchemy as sa

revision = "20260915_evolution_history"
down_revision = "20260915_incense_usage"
branch_labels = None
depends_on = None

def upgrade() -> None:
    for name in ("battle_count", "wild_battle_count", "npc_battle_count", "pvp_battle_count"):
        op.add_column("pokemon", sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
    op.add_column("pokemon", sa.Column("last_battle_at", sa.DateTime(), nullable=True))
    op.add_column("pokemon", sa.Column("last_battle_was_night", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE pokemon_species SET forms = '{\"midday\":{\"name\":\"Lycanroc Midday\"},\"midnight\":{\"name\":\"Lycanroc Midnight\"},\"dusk\":{\"name\":\"Lycanroc Dusk\"}}'::jsonb WHERE national_dex = 745")
    op.execute("""INSERT INTO items (id, name, name_lower, category, description, cost, sell_price, is_purchasable, is_consumable, is_holdable)
        VALUES (35, 'Dusk Rock', 'dusk rock', 'evolution', 'A mysterious rock used by Rockruff for its special night evolution into Lycanroc Midnight.', 5000, 2500, true, false, true)
        ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, name_lower=EXCLUDED.name_lower, category=EXCLUDED.category, description=EXCLUDED.description, cost=EXCLUDED.cost, sell_price=EXCLUDED.sell_price, is_purchasable=EXCLUDED.is_purchasable, is_consumable=EXCLUDED.is_consumable, is_holdable=EXCLUDED.is_holdable""")

def downgrade() -> None:
    for name in ("last_battle_was_night", "last_battle_at", "pvp_battle_count", "npc_battle_count", "wild_battle_count", "battle_count"):
        op.drop_column("pokemon", name)
