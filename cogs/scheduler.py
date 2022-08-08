"""
scheduler.py

Scheduler category and commands.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import re
import warnings
from secrets import token_hex
from typing import TYPE_CHECKING, NamedTuple, Type, Literal, cast, TypeAlias

import aiosqlite
import arrow
from packaging import version

import discord
from discord.ext import commands

from src.commands import Cog
from src.env import COLOUR, SCHEDULER_DATABASE_PATH, DEBUG_MODE, DEFAULT_TIMEZONE, TIME_LANG

if TYPE_CHECKING:
    from src.bot import Bot


logger = logging.getLogger(__name__)

DB_VERSION = 1
TIME_PARSE_METHOD: Literal["dateparser"] | Literal["dateutil"] = "dateparser"  # options: 'dateutil', 'dateparser'
MessageableGuildChannel: TypeAlias = discord.TextChannel | discord.VoiceChannel | discord.Thread


class SanitizedScheduleEvent(NamedTuple):
    """
    Represents a single scheduled message event after modal sanitization.
    """

    author: discord.Member
    channel: MessageableGuildChannel
    message: str
    time: arrow.Arrow
    repeat: float | None


class ScheduleEvent(NamedTuple):
    """
    Represents a single scheduled message event.
    """

    author: discord.Member
    channel: MessageableGuildChannel
    message: str
    time: arrow.Arrow
    repeat: float | None
    mention: bool

    @classmethod
    def from_sanitized(cls, event: SanitizedScheduleEvent, mention: bool) -> ScheduleEvent:
        """
        Converts a SanitizedScheduleEvent to ScheduleEvent.

        :param event: The sanitized event.
        :param mention: Whether mention is allowed.
        :return: The converted ScheduleEvent.
        """
        return cls(event.author, event.channel, event.message, event.time, event.repeat, mention)


class SavedScheduleEvent(NamedTuple):
    """
    Represents a single scheduled message event in DB format.
    """

    id: int
    message: str
    guild_id: int
    channel_id: int
    author_id: int
    next_event_time: int
    repeat: float | int | None
    canceled: bool
    mention: bool

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> SavedScheduleEvent:
        """
        Create a SavedScheduleEvent from a SQLite row.

        :param row: The row fetched from the database.
        :return: Created SavedScheduleEvent.
        """
        return cls(*row)

    def do_repeat(self, current_timestamp: int) -> SavedScheduleEvent:
        """
        Do an iteration of repeat.

        :return: New SavedScheduleEvent with updated next_event_time.
        """
        if self.repeat is None:
            raise ValueError("repeat cannot be None to do_repeat().")
        return SavedScheduleEvent(
            self.id,
            self.message,
            self.guild_id,
            self.channel_id,
            self.author_id,
            int(current_timestamp + self.repeat * 60),
            self.repeat,
            self.canceled,
            self.mention,
        )

    def __lt__(self, other: SavedScheduleEvent) -> bool:  # type: ignore[reportIncompatibleMethodOverride]
        """
        Use next_event_time as the comp.
        """
        return self.next_event_time < other.next_event_time


class TimeInPast(ValueError):
    """
    Raised when scheduler time is in the past.
    """

    def __init__(self, time: arrow.Arrow) -> None:
        self.time = time


class InvalidRepeat(ValueError):
    """
    Raised when scheduler repeat is longer than a year or shorter than an hour.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason


class BadTimezone(ValueError):
    """
    Raised the timezone is invalid.
    """

    def __init__(self, timezone: str | None) -> None:
        """
        :param timezone: The invalid timezone, if timezone is None then
                         it means the timezone is supplied at an invalid location.
        """
        self.timezone = timezone


class BadTimeString(ValueError):
    """
    Raised when the time cannot be parsed.
    """

    def __init__(self, time: str) -> None:
        self.time = time


if TYPE_CHECKING:  # TODO: find another way to fix type checking
    # Provides a stub for ScheduleModal
    class ScheduleModal(discord.ui.Modal, title="Schedule Creator"):
        message: discord.ui.TextInput[ScheduleModal]
        time: discord.ui.TextInput[ScheduleModal]
        timezone: discord.ui.TextInput[ScheduleModal]
        repeat: discord.ui.TextInput[ScheduleModal]

        def __init__(self, scheduler: Scheduler, channel: MessageableGuildChannel) -> None:
            self.scheduler = scheduler
            self.channel = channel
            super().__init__()

        def sanitize_response(self, interaction: discord.Interaction) -> SanitizedScheduleEvent:
            ...

        @property
        def acceptable_formats(self) -> list[str]:
            return []

        async def on_submit(self, interaction: discord.Interaction) -> None:
            ...


def get_schedule_modal(defaults: ScheduleModal | None = None) -> Type[ScheduleModal]:
    """
    This is a class factory to create ScheduleModal with defaults.

    :param defaults: A ScheduleModal object that will be used to populate default fields.
    :return: A class ScheduleModal with defaults.
    """
    message_default = defaults and defaults.message.value
    time_default = defaults and defaults.time.value
    timezone_default = defaults and defaults.timezone.value or DEFAULT_TIMEZONE
    repeat_default = defaults and defaults.repeat.value or "0"

    # noinspection PyShadowingNames
    class ScheduleModal(discord.ui.Modal, title="Schedule Creator"):
        """
        The scheduling modal to collect info for the schedule.
        """

        message: discord.ui.TextInput[ScheduleModal] = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
            default=message_default,
        )
        time: discord.ui.TextInput[ScheduleModal] = discord.ui.TextInput(
            label="Scheduled Time (MM/DD/YY HH:MM:SS)", required=True, max_length=100, default=time_default
        )
        timezone: discord.ui.TextInput[ScheduleModal] = discord.ui.TextInput(
            label="Timezone (UTC offset +/-HH:MM)", required=False, max_length=100, default=timezone_default
        )
        repeat: discord.ui.TextInput[ScheduleModal] = discord.ui.TextInput(
            label="Repeat every n minutes (0 to disable, min 60)",
            required=False,
            max_length=10,
            default=repeat_default,
        )

        def __init__(self, scheduler: Scheduler, channel: MessageableGuildChannel) -> None:
            """
            :param scheduler: The Scheduler object.
            :param channel: The MessageableGuildChannel for the scheduled message.
            """
            self.scheduler = scheduler
            self.channel = channel
            super().__init__()

        def sanitize_response(self, interaction: discord.Interaction) -> SanitizedScheduleEvent:
            """
            Sanitize the modal entries and raise appropriate errors.

            :param interaction: The interaction context.
            :raises ParseError: If the time cannot be understood.
            :raises TimeInPast: If the time is in the past.
            :raises UnknownTimezoneWarning: If the timezone is provided in the time.
            :raises InvalidRepeat: If repeat is longer than a year or shorter than an hour.
            :return: The sanitized ScheduleEvent.
            """

            if self.time.value is None or self.message.value is None:
                raise ValueError("time and message cannot be None here since they are non-optional.")

            if not isinstance(interaction.user, discord.Member):
                raise ValueError("interaction.user must be a Member (cannot be ran from DM).")

            if TIME_PARSE_METHOD == "dateutil":
                from dateutil import parser as du_parser

                try:
                    # parse the time
                    with warnings.catch_warnings():  # will raise exception is an unknown timezone is detected
                        # noinspection PyUnresolvedReferences
                        warnings.simplefilter(
                            "error", du_parser.UnknownTimezoneWarning  # type: ignore[reportGeneralTypeIssues]
                        )  # exists, but editor is weird
                        naive_time = du_parser.parse(self.time.value)
                except du_parser.UnknownTimezoneWarning as e:  # type: ignore[reportGeneralTypeIssues]
                    raise BadTimezone(None) from e
                except du_parser.ParserError as e:  # fails to parse time
                    raise BadTimeString(self.time.value) from e

                # apply the timezone
                if self.timezone.value:  # if user inputted a timezone
                    try:
                        time = arrow.get(naive_time, self.timezone.value)
                    except arrow.ParserError as e:  # fails to parse timezone
                        logger.debug("Failed to parse timezone.", exc_info=e)
                        raise BadTimezone(self.timezone.value) from e
                else:
                    time = arrow.get(naive_time)  # will use either tz from naive time or UTC
                del du_parser  # remove local variable

            else:  # dateparser method
                import dateparser as dp_parser

                try:
                    naive_time = dp_parser.parse(
                        self.time.value,
                        languages=TIME_LANG,
                        settings={
                            "TIMEZONE": self.timezone.value,
                            "RETURN_AS_TIMEZONE_AWARE": True,
                            "DEFAULT_LANGUAGES": TIME_LANG,
                        },  # type: ignore[reportGeneralTypeIssues]
                    )
                except Exception as e:
                    if e.__class__.__name__ == "UnknownTimeZoneError":  # invalid timezone
                        raise BadTimezone(self.timezone.value) from e
                    raise  # re-raise

                if naive_time is None:
                    raise BadTimeString(self.time.value)
                time = arrow.get(naive_time)
                del dp_parser  # remove local variable

            # check time is in the future
            now = arrow.utcnow()
            if time <= now:
                logger.debug("Time is in the past. Time: %s, now: %s", time, now)
                raise TimeInPast(time)

            if not self.repeat.value:
                repeat = None
            else:
                # check repeat is a number
                try:
                    repeat = round(float(self.repeat.value), 2)
                except ValueError:
                    repeat = None
                else:
                    # verify repeat is < year and > one hour
                    if repeat <= 0:
                        repeat = None
                    elif repeat > 60 * 24 * 365:
                        raise InvalidRepeat("Repeat cannot be longer than a year.")
                    elif repeat < (0.2 if DEBUG_MODE else 60):  # 12 seconds for debug mode, 60 min for production
                        if DEBUG_MODE:
                            raise InvalidRepeat("Repeat cannot be less than 12 seconds (debug mode is active).")
                        else:
                            raise InvalidRepeat("Repeat cannot be less than one hour.")

            return SanitizedScheduleEvent(interaction.user, self.channel, self.message.value, time, repeat)

        @property
        def acceptable_formats(self) -> list[str]:
            """
            :return: A list of acceptable time formats.
            """
            return [
                "- 1/30/2023 3:20am",
                "- Jan 30 2023 3:20",
                "- 2023-Jan-30 3h20m",
                "- January 30th, 2023 at 03:20:00",
            ]

        async def on_submit(self, interaction: discord.Interaction) -> None:
            """
            Callback for modal submission.
            """
            try:
                event = self.sanitize_response(interaction)
            except BadTimezone as e:
                if e.timezone is None:
                    # Invalid timezone in dateutil.parser.parse
                    embed = discord.Embed(
                        description="Please don't include timezones in the **Scheduled Time** field.",
                        colour=COLOUR,
                    )
                else:
                    embed = discord.Embed(
                        description="I cannot understand this timezone. Try entering the "
                        "[TZ database name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) "
                        "of your timezone (case sensitive).",
                        colour=COLOUR,
                    )
            except TimeInPast as e:  # time is in the past
                embed = discord.Embed(
                    description=f"The time you inputted is in the past (<t:{int(e.time.timestamp())}>). "
                    f"Double check the time is valid or try one of the formats below.",
                    colour=COLOUR,
                )
                embed.add_field(
                    name="Valid time formats:", value="\n".join(self.acceptable_formats) + "\n- And More..."
                )
            except InvalidRepeat as e:  # repeat is invalid
                embed = discord.Embed(description=e.reason, colour=COLOUR)
            except BadTimeString as e:  # time parse error
                embed = discord.Embed(
                    description=f"I cannot understand the time **{discord.utils.escape_markdown(e.time)}**.",
                    colour=COLOUR,
                )
                embed.add_field(
                    name="Valid time formats:", value="\n".join(self.acceptable_formats) + "\n- And More..."
                )
            else:
                # Check if the message contains a mention and both author
                mentions = re.search(r"@(everyone|here|[!&]?[0-9]{17,20})", event.message)

                if mentions is not None:
                    perms_author = self.channel.permissions_for(event.author)
                    perms_bot = self.channel.permissions_for(self.channel.guild.me)

                    # This is a privileged mention (@everyone, @here, @role)
                    if mentions.group(1) in {"everyone", "here"} or mentions.group(1).startswith("&"):
                        # Bot will need permissions to ping as well
                        check = perms_author.mention_everyone and perms_bot.mention_everyone
                    else:
                        check = perms_author.mention_everyone
                    if check:  # if pinging is a possibility
                        embed = discord.Embed(
                            title="This scheduled message contains mentions",
                            description="Click **Yes** if the mentions should ping "
                            "its members, otherwise click **No**.\n\n"
                            "Alternatively, click **Edit** to revise your message.",
                            colour=COLOUR,
                        )
                        embed.add_field(name="Message", value=event.message, inline=False)
                        await interaction.response.send_message(
                            embed=embed, view=ScheduleMentionView(self, event), ephemeral=True
                        )
                        return

                # Message has no mentions, or the bot or user cannot mention in this channel,
                # so don't bother asking
                await self.scheduler.save_event(interaction, ScheduleEvent.from_sanitized(event, False))
                return

            # If failed
            embed.set_footer(text='Click the "Edit" button below to edit your form.')
            await interaction.response.send_message(embed=embed, view=ScheduleEditView(self), ephemeral=True)

    return ScheduleModal


# The empty ScheduleModal with no defaults
ScheduleModal = get_schedule_modal()


class ScheduleView(discord.ui.View):
    """
    A single-button view for prefixed command to trigger the schedule modal.
    """

    def __init__(self, scheduler: Scheduler, channel: MessageableGuildChannel) -> None:
        """
        :param scheduler: The Scheduler object.
        :param channel: The MessageableGuildChannel for the scheduled message.
        """
        self.scheduler = scheduler
        self.channel = channel
        super().__init__()

    # noinspection PyUnusedLocal
    @discord.ui.button(label="Create", style=discord.ButtonStyle.green)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button[ScheduleView]) -> None:
        """
        The "Create" button for the view.
        """
        await interaction.response.send_modal(ScheduleModal(self.scheduler, self.channel))
        if interaction.message:
            try:
                await interaction.message.edit(view=None)
            finally:  # Somehow fails to edit
                self.stop()


class ScheduleEditView(discord.ui.View):
    """
    A single-button view to allow the user to edit the schedule modal.
    """

    def __init__(self, last_schedule_modal: ScheduleModal) -> None:
        """
        :param last_schedule_modal: The previous ScheduleModal before the retry.
        """
        self.last_schedule_modal = last_schedule_modal
        super().__init__()

    # noinspection PyUnusedLocal
    @discord.ui.button(label="Edit", style=discord.ButtonStyle.green)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button[ScheduleEditView]) -> None:
        """
        The "Edit" button for the view.
        """
        await interaction.response.send_modal(
            get_schedule_modal(self.last_schedule_modal)(
                self.last_schedule_modal.scheduler, self.last_schedule_modal.channel
            )
        )
        self.stop()


class ScheduleMentionView(discord.ui.View):
    """
    A single-button view to ask if the user wishes to mention in their scheduled message.
    """

    def __init__(self, last_schedule_modal: ScheduleModal, event: SanitizedScheduleEvent) -> None:
        """
        :param last_schedule_modal: The previous ScheduleModal before the retry.
        :param event: The sanitized event from the last modal.
        """
        self.last_schedule_modal = last_schedule_modal
        self.event = event
        super().__init__()

    # noinspection PyUnusedLocal
    @discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button[ScheduleMentionView]) -> None:
        """
        The "Yes" button for the view.
        """
        try:
            await self.last_schedule_modal.scheduler.save_event(
                interaction, ScheduleEvent.from_sanitized(self.event, True)
            )
        finally:
            self.stop()

    # noinspection PyUnusedLocal
    @discord.ui.button(label="No", style=discord.ButtonStyle.green)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button[ScheduleMentionView]) -> None:
        """
        The "No" button for the view.
        """
        try:
            await self.last_schedule_modal.scheduler.save_event(
                interaction, ScheduleEvent.from_sanitized(self.event, False)
            )
        finally:
            self.stop()

    # noinspection PyUnusedLocal
    @discord.ui.button(label="Edit", style=discord.ButtonStyle.green)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button[ScheduleMentionView]) -> None:
        """
        The "Edit" button for the view.
        """
        await interaction.response.send_modal(
            get_schedule_modal(self.last_schedule_modal)(
                self.last_schedule_modal.scheduler, self.last_schedule_modal.channel
            )
        )
        self.stop()


class Scheduler(Cog):
    """A general category for all my commands."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.db: aiosqlite.Connection = cast(aiosqlite.Connection, None)
        self.schedule_heap: list[SavedScheduleEvent] = []
        self.heap_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        """
        This is called when cog is loaded.
        """
        # Setup database
        await self.init_db()

        # Populate schedules from database
        schedules: list[SavedScheduleEvent] = []
        async with self.db.execute(
            r"""
            SELECT * 
                FROM Scheduler
                WHERE canceled!=1
                ORDER BY next_event_time
        """
        ) as cur:
            async for row in cur:
                schedules += [SavedScheduleEvent.from_row(row)]

        async with self.heap_lock:
            self.schedule_heap = schedules
            heapq.heapify(self.schedule_heap)

        # Start the scheduler loop
        asyncio.create_task(self.scheduler_event_loop())

    async def cog_unload(self) -> None:
        """
        This is called when cog is unloaded.
        """
        # Close SQLite database
        logger.debug("Closing DB connection.")
        await self.db.close()

    async def _update_to_version_0(self) -> None:
        """
        Update DB to version 0.

        Changes:
          - Create the Scheduler table
          - Add 3 indices to Scheduler
        """
        logger.info("[orange]Updating DB version to 0[/orange]", extra={"markup": True})
        async with self.db.execute(
            r"""
                CREATE TABLE Scheduler (
                    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                    message VARCHAR(1000) NOT NULL,
                    guild_id DECIMAL(22,0) NOT NULL,
                    channel_id DECIMAL(22,0) NOT NULL,
                    author_id DECIMAL(22,0) NOT NULL,
                    next_event_time INTEGER,
                    repeat DOUBLE,
                    canceled BOOLEAN NOT NULL DEFAULT 0 CHECK (canceled IN (0, 1))
                )
            """
        ):
            pass

        async with self.db.execute(
            r"""
                CREATE INDEX IF NOT EXISTS idx_scheduler_time ON Scheduler (next_event_time)
            """
        ):
            pass

        async with self.db.execute(
            r"""
                CREATE INDEX IF NOT EXISTS idx_scheduler_guild_author ON Scheduler (guild_id, author_id)
            """
        ):
            pass

        async with self.db.execute(
            r"""
                CREATE INDEX IF NOT EXISTS idx_scheduler_canceled ON Scheduler (canceled)
            """
        ):
            pass

    async def _update_to_version_1(self) -> None:
        """
        Update DB to version 1.

        Changes:
          - Add version=1 row to Meta table
          - Add mention column to Scheduler
        """
        logger.info("[orange]Updating DB version to 1[/orange]", extra={"markup": True})
        async with self.db.execute(
            r"""
            INSERT INTO Meta(name, value)
            VALUES ('version', 1)
        """
        ):
            pass

        async with self.db.execute(
            r"""
            ALTER TABLE Scheduler 
            ADD COLUMN mention BOOLEAN NOT NULL DEFAULT 0 CHECK (canceled IN (0, 1))
        """
        ):
            pass

    async def init_db(self) -> None:
        """
        Initiates the SQLite database.
        """
        logger.debug("Initiating DB connection.")
        self.db = await aiosqlite.connect(SCHEDULER_DATABASE_PATH)

        # Checks if the meta table exists
        async with self.db.execute(
            r"""
            SELECT name 
                FROM sqlite_master 
                WHERE type='table' 
                    AND name='Meta'
        """
        ) as cur:
            meta_exists = (await cur.fetchone()) is not None

        # If the meta table does not exist, this is means this is the initial database commit
        # or DB version is 0
        if not meta_exists:
            # Create the meta table
            async with self.db.execute(
                r"""
                CREATE TABLE Meta (
                    name VARCHAR(10) PRIMARY KEY NOT NULL,
                    value INTEGER NOT NULL
                )
            """
            ):
                pass

            # Checks if the scheduler table exists
            async with self.db.execute(
                r"""
                    SELECT name 
                        FROM sqlite_master 
                        WHERE type='table' 
                            AND name='Scheduler'
                """
            ) as cur:
                scheduler_exists = (await cur.fetchone()) is not None

            # It's the initial DB commit, this will update to version 0
            if not scheduler_exists:
                await self._update_to_version_0()
            await self._update_to_version_1()

        await self.db.commit()  # commit the changes

    # Older versions don't support RETURNING in SQLite
    if version.parse(aiosqlite.sqlite_version) >= version.parse("3.35.0"):

        async def _insert_schedule(self, event: ScheduleEvent) -> SavedScheduleEvent:
            async with self.db.execute(
                r"""
                    INSERT INTO Scheduler (message, guild_id, channel_id, 
                                           author_id, next_event_time, repeat, mention)
                        VALUES ($message, $guild_id, $channel_id, $author_id, 
                                $next_event_time, $repeat, $mention)
                        RETURNING *
                """,
                {
                    "message": event.message,
                    "guild_id": event.channel.guild.id,
                    "channel_id": event.channel.id,
                    "author_id": event.author.id,
                    "next_event_time": int(event.time.timestamp()),
                    "repeat": event.repeat,
                    "mention": int(event.mention),
                },
            ) as cur:
                row = await cur.fetchone()
                if row is None:
                    raise ValueError("Something went wrong with SQLite, row should not be None.")
                event_db = SavedScheduleEvent.from_row(row)

            await self.db.commit()
            return event_db

    else:

        async def _insert_schedule(self, event: ScheduleEvent) -> SavedScheduleEvent:
            async with self.db.execute(
                r"""
                    INSERT INTO Scheduler (message, guild_id, channel_id, 
                                           author_id, next_event_time, repeat, mention)
                        VALUES ($message, $guild_id, $channel_id, $author_id, 
                                $next_event_time, $repeat, $mention)
                """,
                {
                    "message": event.message,
                    "guild_id": event.channel.guild.id,
                    "channel_id": event.channel.id,
                    "author_id": event.author.id,
                    "next_event_time": int(event.time.timestamp()),
                    "repeat": event.repeat,
                    "mention": int(event.mention),
                },
            ) as cur:
                async with self.db.execute(
                    r"""
                    SELECT * 
                        FROM Scheduler
                        WHERE id=$id
                        LIMIT 1
                """,
                    {"id": cur.lastrowid},
                ) as cur2:
                    row = await cur2.fetchone()
                    if row is None:
                        raise ValueError("Something went wrong with SQLite, row should not be None.")
                    event_db = SavedScheduleEvent.from_row(row)
            await self.db.commit()
            return event_db

    async def save_event(self, interaction: discord.Interaction, event: ScheduleEvent) -> None:
        """
        Saves the ScheduleEvent into database and adds to the event heap.

        :param interaction: The interaction context.
        :param event: The created SanitizedScheduleEvent object from the form.
        """
        try:
            await self._save_event(event)
        except Exception as e:
            # Something unexpected went wrong
            err_code = token_hex(5)
            logger.error("Something went wrong while saving event. Code: %s.", err_code, exc_info=e)
            embed = discord.Embed(
                description="An unexpected error occurred, try again later. "
                f"Please report this to the bot author with error code `{err_code}`.",
                colour=COLOUR,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="Scheduled Message Created",
            colour=COLOUR,
        )
        embed.add_field(name="Message", value=event.message, inline=False)
        embed.add_field(name="Channel", value=event.channel.mention, inline=True)
        if event.repeat is None:
            embed.add_field(name="Repeat", value=f"Disabled", inline=True)
        else:
            if event.repeat.is_integer():
                repeat_message = f"Every {int(event.repeat)} minute{'s' if event.repeat != 1 else ''}"
            else:
                repeat_message = f"Every {event.repeat:.2f} minute{'s' if event.repeat != 1 else ''}"
            embed.add_field(name="Repeat", value=repeat_message, inline=True)

        mentions = re.search(r"@(everyone|here|[!&]?[0-9]{17,20})", event.message)
        if mentions is not None:  # has mentions
            embed.add_field(name="Ping Enabled", value="Yes" if event.mention else "No", inline=True)
        embed.add_field(name="Time", value=f"<t:{int(event.time.timestamp())}>", inline=False)

        embed.set_footer(text=f"{event.author} has created a scheduled message.")
        await interaction.response.send_message(embed=embed)
        return

    async def _save_event(self, event: ScheduleEvent) -> None:
        """
        Saves the ScheduleEvent into database and adds to the event heap.

        :param event: The created ScheduleEvent object from the form.
        """
        # Inserts into database
        event_db = await self._insert_schedule(event)

        logger.info("Added schedule into database with ID %d.", event_db.id)
        logger.info(
            "Message (preview): %s\nGuild: %s\nChannel: %s\nAuthor: %s\nRepeat: %s\nMention: %s\nTime: %s",
            event.message[:80],
            event.channel.guild,
            event.channel,
            event.author,
            event.repeat,
            event.mention,
            event.time,
        )

        # Add the event into the schedule heap
        async with self.heap_lock:
            heapq.heappush(self.schedule_heap, event_db)

    async def send_scheduled_message(self, event: SavedScheduleEvent) -> bool:
        """
        Sends a scheduled event message.

        :param event: A SavedScheduleEvent fetched from the database.
        :return: True if send was successful, False otherwise.
        """

        # Check if the event was canceled
        async with self.db.execute(
            r"""
            SELECT canceled 
                FROM Scheduler
                WHERE id=$id
        """,
            {"id": event.id},
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                logger.error("Row should not be None, why was this deleted?")
                return False

            if row[0]:  # if canceled is true
                logger.warning("Event with ID %d was canceled.", event.id)
                return False

        # Check if bot is still in guild
        guild = self.bot.get_guild(event.guild_id)
        if not guild:
            logger.warning("Event with ID %d guild not found.", event.id)
            return False

        # Check if channel still exists
        channel = guild.get_channel_or_thread(event.channel_id)
        if not channel:
            logger.warning("Event with ID %d channel not found.", event.id)
            return False
        if not hasattr(channel, "send"):
            logger.warning("Event with ID %d channel is not a messageable channel.", event.id)
            return False

        # Check if user is still in guild
        author = guild.get_member(event.author_id)
        if not author:
            try:
                author = await guild.fetch_member(event.author_id)
            except discord.NotFound:
                logger.warning("Event with ID %d author not found.", event.id)
                return False

        # Check if the still user has permission
        perms_author = channel.permissions_for(author)
        if not perms_author.read_messages or not perms_author.send_messages:
            logger.warning("Event with ID %d author doesn't have perms.", event.id)
            return False

        # Check if the bot still has permission
        perms_bot = channel.permissions_for(guild.me)
        if not perms_bot.read_messages or not perms_bot.send_messages:
            logger.warning("Event with ID %d bot doesn't have perms.", event.id)
            return False

        if event.mention and perms_author.mention_everyone:  # if mentions is enabled and author still has perms
            allowed_mentions = discord.AllowedMentions.all()
        else:
            if event.mention:
                logger.debug(
                    "Event with ID %s mention disabled due to author doesn't have mention_everyone permission.",
                    event.id,
                )
            allowed_mentions = discord.AllowedMentions.none()
        # channel has .send since invalid channel typed are filtered above with hasattr(channel, 'send')
        await channel.send(event.message,  # type: ignore[reportGeneralTypeIssues]
                           allowed_mentions=allowed_mentions)
        # TODO: add a "report abuse" feature/command, save all sent msg in a db table with the id
        return True

    async def _scheduler_event_loop(self) -> None:
        """
        Internal iteration of the scheduler event loop.
        """
        should_sleep = False
        while not should_sleep:
            should_sleep = True

            if self.schedule_heap:
                async with self.heap_lock:  # pop the next event from heap
                    next_event = heapq.heappop(self.schedule_heap)

                now = arrow.utcnow().timestamp()
                # Time has past
                if next_event.next_event_time < now:
                    should_sleep = False
                    try:
                        # Attempt to send the message
                        success = await self.send_scheduled_message(next_event)
                    except Exception as e:
                        # Something unexpected went wrong
                        logger.error(
                            "Something went wrong while sending the scheduled message with event ID %d.",
                            next_event.id,
                            exc_info=e,
                        )
                        success = False

                    if not success or next_event.repeat is None:
                        # If the message failed to send or the message isn't on repeat, then cancel the schedule
                        async with self.db.execute(
                            r"""
                                UPDATE Scheduler
                                    SET canceled=1
                                    WHERE id=$id
                            """,
                            {"id": next_event.id},
                        ):
                            pass
                        await self.db.commit()

                    else:
                        # Otherwise, update the next_event_time
                        new_event = next_event.do_repeat(int(now))
                        async with self.db.execute(
                            r"""
                                UPDATE Scheduler
                                    SET next_event_time=$next_event_time
                                    WHERE id=$id
                            """,
                            {"next_event_time": new_event.next_event_time, "id": next_event.id},
                        ):
                            pass
                        await self.db.commit()
                        # re-add the updated event
                        async with self.heap_lock:
                            heapq.heappush(self.schedule_heap, new_event)
                else:
                    # re-add the original event when the time isn't up yet
                    async with self.heap_lock:
                        heapq.heappush(self.schedule_heap, next_event)

    async def scheduler_event_loop(self) -> None:
        """
        The main scheduler event loop, checks every second.
        """
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                await self._scheduler_event_loop()
            except Exception as e:
                logger.error("An uncaught error was raised during scheduled event loop.", exc_info=e)
            await asyncio.sleep(1)

    @commands.guild_only()
    @commands.hybrid_group(fallback="create", ignore_extra=False)
    @discord.app_commands.describe(channel="The channel for the scheduled message.")
    async def schedule(
        self,
        ctx: commands.Context[Bot],
        *,
        channel: MessageableGuildChannel = None,  # type: ignore[reportGeneralTypeIssues]
    ) -> None:
        """Schedules a message for the future.
        `channel` - The channel for the scheduled message.

        You must have **send messages** permissions in the target channel.
        """

        if channel is None:
            if not isinstance(ctx.channel, MessageableGuildChannel):
                raise ValueError("Where else was this command ran?")

            channel = ctx.channel

        if not isinstance(ctx.author, discord.Member):
            raise ValueError("How does a non-member run this command?")

        if not isinstance(ctx.me, discord.Member):
            raise ValueError("Why am I not a member?")

        # Check if the user has permission
        perms = channel.permissions_for(ctx.author)
        if not perms.read_messages or not perms.send_messages:
            embed = discord.Embed(
                description=f"You must have **send messages** permissions in {channel.mention}.", colour=COLOUR
            )
            await ctx.reply(embed=embed)
            return
        # Check if the bot has permission
        perms = channel.permissions_for(ctx.me)
        if not perms.read_messages or not perms.send_messages:
            embed = discord.Embed(description=f"I don't have permission in {channel.mention}.", colour=COLOUR)
            await ctx.reply(embed=embed)
            return

        # If prefixed command is used, send a button
        if ctx.interaction is None:
            embed = discord.Embed(
                description="Click the button below to create a scheduled message.", colour=COLOUR
            )
            await ctx.reply(embed=embed, view=ScheduleView(self, channel))
        else:
            # Otherwise, directly open the modal
            await ctx.interaction.response.send_modal(ScheduleModal(self, channel))

    @commands.guild_only()
    @schedule.command(name="create", with_app_command=False, ignore_extra=False, hidden=True)
    async def schedule_create(
        self,
        ctx: commands.Context[Bot],
        *,
        channel: MessageableGuildChannel = None,  # type: ignore[reportGeneralTypeIssues]
    ) -> None:
        """Schedules a message for the future.
        `channel` - The channel for the scheduled message.

        You must have **send messages** permissions in the target channel.
        """
        # This command is an alias to `schedule`
        await ctx.invoke(self.schedule.callback, ctx, channel=channel)  # type: ignore[reportGeneralTypeIssues]

    @commands.guild_only()
    @schedule.command(name="list", ignore_extra=False)
    @discord.app_commands.describe(channel="The channel to list scheduled messages.")
    async def schedule_list(
        self,
        ctx: commands.Context[Bot],
        *,
        channel: MessageableGuildChannel = None,  # type: ignore[reportGeneralTypeIssues]
    ) -> None:
        """List your scheduled messages in this server or a specific channel.
        `channel` - The channel to list scheduled messages.
        """

        if channel is None:
            if not isinstance(ctx.channel, MessageableGuildChannel):
                raise ValueError("Where else was this command ran?")

            channel = ctx.channel

    @commands.guild_only()
    @schedule.command(name="remove", aliases=["delete"])
    @discord.app_commands.describe(event_id="The event ID of the scheduled message (see `/list`).")
    async def schedule_remove(self, ctx: commands.Context[Bot], event_id: int) -> None:
        """Remove a previously scheduled message event.
        `channel` - The channel of the scheduled message.
        `event_id` - The event ID of the scheduled message (see `/list`).
        """
        ...


async def setup(bot: Bot) -> None:
    await bot.add_cog(Scheduler(bot))
