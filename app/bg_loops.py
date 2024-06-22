from __future__ import annotations

import asyncio
import time

import app.packets
import app.settings
import app.state
from app.constants.privileges import Privileges
from app.logging import Ansi
from app.logging import log

OSU_CLIENT_MIN_PING_INTERVAL = 300000 // 1000  # defined by osu!


async def initialize_housekeeping_tasks() -> None:
    """Create tasks for each housekeeping tasks."""
    log("Initializing housekeeping tasks.", Ansi.LCYAN)

    loop = asyncio.get_running_loop()

    app.state.sessions.housekeeping_tasks.update(
        {
            loop.create_task(task)
            for task in (
                _remove_expired_donation_privileges(interval=30 * 60),
                _update_bot_status(interval=5 * 60),
                _disconnect_ghosts(interval=OSU_CLIENT_MIN_PING_INTERVAL // 3),
                _remove_expired_tournament_match(interval=60 * 5)
            )
        },
    )


async def _remove_expired_donation_privileges(interval: int) -> None:
    """Remove donation privileges from users with expired sessions."""
    while True:
        if app.settings.DEBUG:
            log("Removing expired donation privileges.", Ansi.LMAGENTA)

        expired_donors = await app.state.services.database.fetch_all(
            "SELECT id FROM users "
            "WHERE donor_end <= UNIX_TIMESTAMP() "
            "AND priv & :donor_priv",
            {"donor_priv": Privileges.DONATOR.value},
        )

        for expired_donor in expired_donors:
            player = await app.state.sessions.players.from_cache_or_sql(
                id=expired_donor["id"],
            )

            assert player is not None

            # TODO: perhaps make a `revoke_donor` method?
            await player.remove_privs(Privileges.DONATOR)
            player.donor_end = 0
            await app.state.services.database.execute(
                "UPDATE users SET donor_end = 0 WHERE id = :id",
                {"id": player.id},
            )

            if player.is_online:
                player.enqueue(
                    app.packets.notification("Your supporter status has expired."),
                )

            log(f"{player}'s supporter status has expired.", Ansi.LMAGENTA)

        await asyncio.sleep(interval)


async def _disconnect_ghosts(interval: int) -> None:
    """Actively disconnect users above the
    disconnection time threshold on the osu! server."""
    while True:
        await asyncio.sleep(interval)
        current_time = time.time()

        for player in app.state.sessions.players:
            if current_time - player.last_recv_time > OSU_CLIENT_MIN_PING_INTERVAL:
                log(f"Auto-dced {player}.", Ansi.LMAGENTA)
                player.logout()


async def _update_bot_status(interval: int) -> None:
    """Re roll the bot status, every `interval`."""
    while True:
        await asyncio.sleep(interval)
        app.packets.bot_stats.cache_clear()


async def _remove_expired_tournament_match(interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        current_time = time.time()
        
        for match in app.state.sessions.matches:
            if not match.is_tournament_match:
                continue
            
            if all(s.empty() for s in match.slots):
                if current_time - match.tournament_remove_last_check_time > 3600:
                    # multi is now empty and is a tournament match, chat has been removed.
                    # remove the multi from the channels list.
                    log(f"Tournament match {match} finished.")

                    # cancel any pending start timers
                    if match.starting is not None:
                        match.starting["start"].cancel()
                        for alert in match.starting["alerts"]:
                            alert.cancel()

                        match.starting = None

                    app.state.sessions.matches.remove(match)

                    lobby = app.state.sessions.channels.get_by_name("#lobby")
                    if lobby:
                        lobby.enqueue(app.packets.dispose_match(match.id))
            else:
                match.tournament_remove_last_check_time = current_time