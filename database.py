# database.py
import asyncpg
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import logging

logger = logging.getLogger(__name__)


class DatabaseManager:
    def __init__(self):
        self.pool = None
        self.database_url = os.getenv("DATABASE_URL")

    async def initialize(self):
        """Initialize the database connection pool"""
        if not self.database_url:
            raise ValueError("DATABASE_URL environment variable not set")

        try:
            self.pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=10,
                command_timeout=60
            )
            await self.create_tables()
            logger.info("Database connection pool initialized")
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise

    async def create_tables(self):
        """Create all necessary tables"""
        async with self.pool.acquire() as conn:
            # User statistics table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(255),
                    sales INTEGER DEFAULT 0,
                    purchases INTEGER DEFAULT 0,
                    total_rating INTEGER DEFAULT 0,
                    rating_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Active listings table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS active_listings (
                    listing_id VARCHAR(255) PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    account_type VARCHAR(255),
                    price VARCHAR(100),
                    description TEXT,
                    image_url TEXT,
                    listing_channel_id BIGINT,
                    listing_message_id BIGINT,
                    extra_message_ids TEXT, -- JSON array of message IDs
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP
                )
            ''')

            # Temporary sales table (for pending image submissions)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS temp_sales (
                    user_id BIGINT PRIMARY KEY,
                    account_type VARCHAR(255),
                    price VARCHAR(100),
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP
                )
            ''')

            # Trade history table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS trade_history (
                    trade_id VARCHAR(255) PRIMARY KEY,
                    buyer_id BIGINT NOT NULL,
                    seller_id BIGINT NOT NULL,
                    account_type VARCHAR(255),
                    price VARCHAR(100),
                    description TEXT,
                    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    trade_channel_id BIGINT
                )
            ''')

            # Vouches/Reviews table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS vouches (
                    id SERIAL PRIMARY KEY,
                    trade_id VARCHAR(255) NOT NULL,
                    rater_id BIGINT NOT NULL,
                    rated_user_id BIGINT NOT NULL,
                    rating INTEGER CHECK (rating >= 1 AND rating <= 5),
                    comment TEXT,
                    role VARCHAR(20) CHECK (role IN ('buyer', 'seller')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(trade_id, rater_id)
                )
            ''')

            # Pending vouches table (for incomplete rating pairs)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS pending_vouches (
                    trade_id VARCHAR(255),
                    role VARCHAR(20) CHECK (role IN ('buyer', 'seller')),
                    rater_id BIGINT NOT NULL,
                    rated_user_id BIGINT NOT NULL,
                    rating INTEGER CHECK (rating >= 1 AND rating <= 5),
                    comment TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (trade_id, role)
                )
            ''')

            # Support tickets table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS support_tickets (
                    ticket_id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    channel_id BIGINT UNIQUE,
                    status VARCHAR(20) DEFAULT 'open' CHECK (status IN ('open', 'closed')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    closed_by BIGINT
                )
            ''')

            # Create indexes for better performance
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_vouches_rated_user ON vouches(rated_user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_vouches_trade ON vouches(trade_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_listings_user ON active_listings(user_id)')
            await conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_trade_history_users ON trade_history(buyer_id, seller_id)')

        logger.info("Database tables created/verified successfully")

    async def close(self):
        """Close the database connection pool"""
        if self.pool:
            await self.pool.close()
            logger.info("Database connection pool closed")

    # === USER STATS METHODS ===
    async def get_user_stats(self, user_id: int, username: str = None) -> Dict:
        """Get user trading statistics"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM user_stats WHERE user_id = $1',
                user_id
            )

            if row:
                return dict(row)
            else:
                # Create new user stats if they don't exist
                if username:
                    await conn.execute('''
                        INSERT INTO user_stats (user_id, username) 
                        VALUES ($1, $2)
                        ON CONFLICT (user_id) DO UPDATE SET username = $2
                    ''', user_id, username)

                return {
                    "user_id": user_id,
                    "username": username,
                    "sales": 0,
                    "purchases": 0,
                    "total_rating": 0,
                    "rating_count": 0
                }

    async def update_user_stats(self, user_id: int, action: str, rating: int = None, username: str = None):
        """Update user statistics"""
        async with self.pool.acquire() as conn:
            # Ensure user exists
            await conn.execute('''
                INSERT INTO user_stats (user_id, username) 
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET 
                    username = COALESCE($2, user_stats.username),
                    updated_at = CURRENT_TIMESTAMP
            ''', user_id, username)

            if action in ["sale", "sales"]:
                await conn.execute('''
                    UPDATE user_stats 
                    SET sales = sales + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                ''', user_id)
            elif action in ["purchase", "purchases"]:
                await conn.execute('''
                    UPDATE user_stats 
                    SET purchases = purchases + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                ''', user_id)

            if rating is not None:
                await conn.execute('''
                    UPDATE user_stats 
                    SET total_rating = total_rating + $2, 
                        rating_count = rating_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                ''', user_id, rating)

    async def get_average_rating(self, user_id: int) -> float:
        """Calculate user's average rating"""
        stats = await self.get_user_stats(user_id)
        if stats["rating_count"] == 0:
            return 0
        return round(stats["total_rating"] / stats["rating_count"], 1)

    # === LISTING METHODS ===
    async def save_temp_sale(self, user_id: int, sale_data: Dict):
        """Save temporary sale data"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO temp_sales (user_id, account_type, price, description, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE SET
                    account_type = $2,
                    price = $3,
                    description = $4,
                    expires_at = $5,
                    created_at = CURRENT_TIMESTAMP
            ''', user_id, sale_data["account_type"], sale_data["price"],
                               sale_data["description"], sale_data["expires_at"])

    async def get_temp_sale(self, user_id: int) -> Optional[Dict]:
        """Get temporary sale data"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM temp_sales WHERE user_id = $1',
                user_id
            )
            return dict(row) if row else None

    async def delete_temp_sale(self, user_id: int):
        """Delete temporary sale data"""
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM temp_sales WHERE user_id = $1', user_id)

    async def create_active_listing(sale_data: dict):
        query = """
        INSERT INTO active_listings (
            listing_id, account_type, price, description,
            user_id, listing_channel_id, listing_message_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """
        await conn.execute(
            query,
            sale_data["listing_id"],
            sale_data["account_type"],
            sale_data["price"],
            sale_data["description"],
            sale_data["user_id"],
            sale_data["listing_channel_id"],
            sale_data["listing_message_id"],
        )


    async def update_active_listing(self, listing_data: dict):
        """Update an active listing in the database"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE active_listings 
                SET account_type = $1, price = $2, description = $3, updated_at = $4
                WHERE listing_id = $5
            ''',
                               listing_data["account_type"],
                               listing_data["price"],
                               listing_data["description"],
                               datetime.utcnow(),
                               listing_data["listing_id"]
                               )

    async def get_active_listing(self, listing_id: str) -> Optional[Dict]:
        """Get active listing by ID"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM active_listings WHERE listing_id = $1',
                listing_id
            )
            if row:
                data = dict(row)
                data["extra_message_ids"] = json.loads(data["extra_message_ids"] or "[]")
                return data
            return None

    async def delete_active_listing(self, listing_id: str):
        """Delete active listing"""
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM active_listings WHERE listing_id = $1', listing_id)

    async def get_user_active_listings(self, user_id: int) -> List[Dict]:
        """Get all active listings for a user"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT * FROM active_listings WHERE user_id = $1',
                user_id
            )
            return [dict(row) for row in rows]

    # === VOUCH/RATING METHODS ===
    async def save_pending_vouch(self, trade_id: str, role: str, rater_id: int,
                                 rated_user_id: int, rating: int, comment: str):
        """Save pending vouch"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO pending_vouches 
                (trade_id, role, rater_id, rated_user_id, rating, comment)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (trade_id, role) DO UPDATE SET
                    rater_id = $3,
                    rated_user_id = $4,
                    rating = $5,
                    comment = $6,
                    created_at = CURRENT_TIMESTAMP
            ''', trade_id, role, rater_id, rated_user_id, rating, comment)

    async def get_pending_vouches(self, trade_id: str) -> Dict:
        """Get all pending vouches for a trade"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT * FROM pending_vouches WHERE trade_id = $1',
                trade_id
            )
            return {row["role"]: dict(row) for row in rows}

    async def complete_vouch(self, trade_id: str):
        """Move pending vouches to completed vouches"""
        async with self.pool.acquire() as conn:
            # Get pending vouches
            pending = await conn.fetch(
                'SELECT * FROM pending_vouches WHERE trade_id = $1',
                trade_id
            )

            # Move to completed vouches
            for vouch in pending:
                await conn.execute('''
                    INSERT INTO vouches 
                    (trade_id, rater_id, rated_user_id, rating, comment, role)
                    VALUES ($1, $2, $3, $4, $5, $6)
                ''', vouch["trade_id"], vouch["rater_id"], vouch["rated_user_id"],
                                   vouch["rating"], vouch["comment"], vouch["role"])

            # Remove pending vouches
            await conn.execute('DELETE FROM pending_vouches WHERE trade_id = $1', trade_id)

    async def delete_pending_vouch(self, trade_id: str):
        """Delete pending vouches for a trade"""
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM pending_vouches WHERE trade_id = $1', trade_id)

    # === TRADE HISTORY METHODS ===
    async def save_trade_history(self, trade_id: str, buyer_id: int, seller_id: int,
                                 account_type: str, price: str, description: str,
                                 trade_channel_id: int = None):
        """Save completed trade to history"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO trade_history 
                (trade_id, buyer_id, seller_id, account_type, price, description, trade_channel_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            ''', trade_id, buyer_id, seller_id, account_type, price, description, trade_channel_id)

    # === TICKET METHODS ===
    async def create_ticket(self, user_id: int, channel_id: int) -> int:
        """Create a new support ticket"""
        async with self.pool.acquire() as conn:
            ticket_id = await conn.fetchval('''
                INSERT INTO support_tickets (user_id, channel_id)
                VALUES ($1, $2)
                RETURNING ticket_id
            ''', user_id, channel_id)
            return ticket_id

    async def close_ticket(self, channel_id: int, closed_by: int):
        """Close a support ticket"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE support_tickets 
                SET status = 'closed', closed_at = CURRENT_TIMESTAMP, closed_by = $2
                WHERE channel_id = $1
            ''', channel_id, closed_by)

    async def get_open_tickets_count(self) -> int:
        """Get count of open tickets"""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                'SELECT COUNT(*) FROM support_tickets WHERE status = $1',
                'open'
            )
            return count or 0

    async def get_total_tickets_count(self) -> int:
        """Get total count of all tickets"""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval('SELECT COUNT(*) FROM support_tickets')
            return count or 0

    # === CLEANUP METHODS ===
    async def cleanup_expired_temp_sales(self):
        """Remove expired temporary sales"""
        async with self.pool.acquire() as conn:
            deleted = await conn.execute('''
                DELETE FROM temp_sales 
                WHERE expires_at <= CURRENT_TIMESTAMP
            ''')
            return int(deleted.split()[-1]) if deleted else 0

    async def cleanup_expired_listings(self, hours: int = 72):
        """Remove old listings (default 72 hours)"""
        async with self.pool.acquire() as conn:
            cutoff = datetime.utcnow() - timedelta(hours=hours)
            deleted = await conn.execute('''
                DELETE FROM active_listings 
                WHERE created_at <= $1
            ''', cutoff)
            return int(deleted.split()[-1]) if deleted else 0

    # === ADMIN METHODS ===
    async def clear_user_vouches(self, user_id: int):
        """Clear all vouch data for a user"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Clear ratings from user stats
                await conn.execute('''
                    UPDATE user_stats 
                    SET total_rating = 0, rating_count = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                ''', user_id)

                # Remove completed vouches
                await conn.execute('DELETE FROM vouches WHERE rated_user_id = $1', user_id)

                # Remove pending vouches
                await conn.execute('DELETE FROM pending_vouches WHERE rated_user_id = $1', user_id)

    async def reset_user_completely(self, user_id: int):
        """Completely reset all user data"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Delete from all tables
                await conn.execute('DELETE FROM user_stats WHERE user_id = $1', user_id)
                await conn.execute('DELETE FROM active_listings WHERE user_id = $1', user_id)
                await conn.execute('DELETE FROM temp_sales WHERE user_id = $1', user_id)
                await conn.execute('DELETE FROM vouches WHERE rater_id = $1 OR rated_user_id = $1', user_id)
                await conn.execute('DELETE FROM pending_vouches WHERE rater_id = $1 OR rated_user_id = $1', user_id)
                # Note: We keep trade_history for audit purposes

    # === LOADING METHODS (for bot startup) ===
    async def load_all_data(self) -> Dict[str, Any]:
        """Load all data for bot initialization"""
        async with self.pool.acquire() as conn:
            # Load user stats
            user_stats_rows = await conn.fetch('SELECT * FROM user_stats')
            user_stats = {}
            for row in user_stats_rows:
                user_stats[row["user_id"]] = {
                    "sales": row["sales"],
                    "purchases": row["purchases"],
                    "total_rating": row["total_rating"],
                    "rating_count": row["rating_count"]
                }

            # Load active listings
            active_listings_rows = await conn.fetch('SELECT * FROM active_listings')
            active_listings = {}
            for row in active_listings_rows:
                listing_data = dict(row)
                listing_data["extra_message_ids"] = json.loads(listing_data["extra_message_ids"] or "[]")
                active_listings[row["listing_id"]] = listing_data

            # Load pending vouches
            pending_vouches_rows = await conn.fetch('SELECT * FROM pending_vouches')
            pending_vouches = {}
            for row in pending_vouches_rows:
                trade_id = row["trade_id"]
                if trade_id not in pending_vouches:
                    pending_vouches[trade_id] = {}
                pending_vouches[trade_id][row["role"]] = dict(row)

            return {
                "user_stats": user_stats,
                "active_listings": active_listings,
                "pending_vouches": pending_vouches
            }


# Global database instance
db = DatabaseManager()