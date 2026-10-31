import os
import sqlite3
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("termination-bot")

TOKEN = (os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN") or "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
REVIEW_CHANNEL_ID = int(os.getenv("REVIEW_CHANNEL_ID", "0") or 0)
TERMINATED_ROLE_ID = int(os.getenv("TERMINATED_ROLE_ID", "0") or 0)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
REVIEWER_ROLE_IDS = {
    int(x.strip()) for x in os.getenv("REVIEWER_ROLE_IDS", "").split(",")
    if x.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "/app/data/terminations.sqlite3")
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

if not TOKEN:
    raise RuntimeError("Не задан DISCORD_TOKEN в файле .env")

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("""
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    game_nick TEXT NOT NULL,
    static_id TEXT NOT NULL,
    rank TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    review_message_id INTEGER,
    reviewer_id INTEGER,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
)
""")
db.commit()


def reviewer_allowed(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.id in REVIEWER_ROLE_IDS for role in member.roles)


def make_embed(row, status="pending"):
    colors = {
        "pending": discord.Color.orange(),
        "approved": discord.Color.green(),
        "rejected": discord.Color.red(),
    }
    labels = {
        "pending": "Ожидает рассмотрения",
        "approved": "Одобрено — сотрудник уволен",
        "rejected": "Отклонено",
    }
    embed = discord.Embed(
        title=f"Заявление на увольнение №{row['id']}",
        color=colors.get(status, discord.Color.dark_grey()),
        timestamp=datetime.fromisoformat(row["created_at"])
    )
    embed.add_field(name="Пользователь Discord", value=f"<@{row['user_id']}> (`{row['user_id']}`)", inline=False)
    embed.add_field(name="Игровой никнейм", value=row["game_nick"], inline=True)
    embed.add_field(name="Static ID", value=row["static_id"], inline=True)
    embed.add_field(name="Текущее звание", value=row["rank"], inline=True)
    embed.add_field(name="Причина увольнения", value=row["reason"], inline=False)
    embed.add_field(name="Статус", value=labels.get(status, status), inline=False)
    if row["reviewer_id"]:
        embed.add_field(name="Рассмотрел", value=f"<@{row['reviewer_id']}>", inline=False)
    embed.set_footer(text="ГИБДД | RMRP «Тверской» • Система увольнений")
    return embed


class ResignationModal(discord.ui.Modal, title="Заявление на увольнение"):
    game_nick = discord.ui.TextInput(
        label="Игровой никнейм", placeholder="Например: Иван Иванов",
        min_length=2, max_length=64
    )
    static_id = discord.ui.TextInput(
        label="Static ID", placeholder="Введите ваш Static ID",
        min_length=1, max_length=24
    )
    rank = discord.ui.TextInput(
        label="Текущее звание", placeholder="Например: Младший сержант полиции",
        min_length=2, max_length=80
    )
    reason = discord.ui.TextInput(
        label="Причина увольнения", style=discord.TextStyle.paragraph,
        placeholder="Укажите причину увольнения...",
        min_length=5, max_length=1000
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "Подать заявление можно только на сервере.", ephemeral=True
            )
        if not REVIEW_CHANNEL_ID:
            return await interaction.response.send_message(
                "Администратор ещё не настроил канал рассмотрения заявлений.", ephemeral=True
            )
        channel = interaction.guild.get_channel(REVIEW_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "Канал рассмотрения не найден. Сообщите администрации.", ephemeral=True
            )

        created = datetime.now(timezone.utc).isoformat()
        cur = db.execute(
            """INSERT INTO applications
            (guild_id,user_id,game_nick,static_id,rank,reason,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (interaction.guild.id, interaction.user.id, str(self.game_nick),
             str(self.static_id), str(self.rank), str(self.reason), created)
        )
        app_id = cur.lastrowid
        db.commit()
        row = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()

        try:
            message = await channel.send(embed=make_embed(row), view=ReviewView(app_id))
            db.execute("UPDATE applications SET review_message_id=? WHERE id=?",
                       (message.id, app_id))
            db.commit()
        except discord.Forbidden:
            db.execute("UPDATE applications SET status='delivery_error' WHERE id=?", (app_id,))
            db.commit()
            return await interaction.response.send_message(
                "Не удалось отправить заявление. У бота нет прав писать в канал рассмотрения.",
                ephemeral=True
            )

        await interaction.response.send_message(
            f"Заявление №**{app_id}** отправлено руководству на рассмотрение.",
            ephemeral=True
        )


class ApplicationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Подать заявление на увольнение",
        style=discord.ButtonStyle.danger,
        custom_id="resignation:open"
    )
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ResignationModal())


class ReviewView(discord.ui.View):
    def __init__(self, application_id: int):
        super().__init__(timeout=None)
        self.application_id = application_id
        self.approve.custom_id = f"resignation:approve:{application_id}"
        self.reject.custom_id = f"resignation:reject:{application_id}"

    async def finish(self, interaction: discord.Interaction, approved: bool):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Недоступно вне сервера.", ephemeral=True)
        if not reviewer_allowed(interaction.user):
            return await interaction.response.send_message(
                "У вас нет прав рассматривать заявления.", ephemeral=True
            )

        row = db.execute("SELECT * FROM applications WHERE id=?", (self.application_id,)).fetchone()
        if not row:
            return await interaction.response.send_message("Заявление не найдено.", ephemeral=True)
        if row["status"] != "pending":
            return await interaction.response.send_message(
                "Это заявление уже рассмотрено.", ephemeral=True
            )

        guild = interaction.guild
        member = guild.get_member(row["user_id"])
        if approved:
            if member is None:
                try:
                    member = await guild.fetch_member(row["user_id"])
                except discord.NotFound:
                    return await interaction.response.send_message(
                        "Пользователь уже отсутствует на сервере. Заявление не изменено.",
                        ephemeral=True
                    )
            terminated_role = guild.get_role(TERMINATED_ROLE_ID) if TERMINATED_ROLE_ID else None
            if terminated_role is None:
                return await interaction.response.send_message(
                    "Не настроена роль «Уволен». Укажите TERMINATED_ROLE_ID в .env.",
                    ephemeral=True
                )
            me = guild.me
            if me is None or not me.guild_permissions.manage_roles or not me.guild_permissions.manage_nicknames:
                return await interaction.response.send_message(
                    "Боту нужны права «Управлять ролями» и «Управлять никами».",
                    ephemeral=True
                )
            if terminated_role >= me.top_role:
                return await interaction.response.send_message(
                    "Роль «Уволен» должна находиться ниже высшей роли бота.",
                    ephemeral=True
                )
            removable = [r for r in member.roles if r != guild.default_role and r < me.top_role]
            blocked = [r for r in member.roles if r != guild.default_role and r >= me.top_role]
            try:
                if removable:
                    await member.remove_roles(*removable, reason=f"Увольнение по заявлению №{self.application_id}")
                if terminated_role not in member.roles:
                    await member.add_roles(terminated_role, reason=f"Увольнение по заявлению №{self.application_id}")
                old_nick = member.nick or member.name
                new_nick = old_nick if old_nick.startswith("[УВОЛЕН]") else f"[УВОЛЕН] {old_nick}"
                await member.edit(nick=new_nick[:32], reason=f"Увольнение по заявлению №{self.application_id}")
            except discord.Forbidden:
                return await interaction.response.send_message(
                    "Discord запретил изменить роли или ник. Проверьте иерархию ролей и права бота.",
                    ephemeral=True
                )
            except discord.HTTPException as exc:
                return await interaction.response.send_message(
                    f"Discord не выполнил действие: {exc}", ephemeral=True
                )
            status = "approved"
            extra = ""
            if blocked:
                extra = "\n⚠️ Некоторые роли выше или на уровне роли бота снять не удалось."
        else:
            status = "rejected"
            extra = ""

        db.execute(
            "UPDATE applications SET status=?, reviewer_id=?, reviewed_at=? WHERE id=?",
            (status, interaction.user.id, datetime.now(timezone.utc).isoformat(), self.application_id)
        )
        db.commit()
        updated = db.execute("SELECT * FROM applications WHERE id=?", (self.application_id,)).fetchone()
        await interaction.response.edit_message(embed=make_embed(updated, status), view=None)
        if extra:
            await interaction.followup.send(extra, ephemeral=True)

        if LOG_CHANNEL_ID:
            log_channel = guild.get_channel(LOG_CHANNEL_ID)
            if isinstance(log_channel, discord.TextChannel):
                action = "одобрил увольнение" if approved else "отклонил заявление"
                await log_channel.send(
                    f"Заявление №{self.application_id}: {interaction.user.mention} {action}. "
                    f"Сотрудник: <@{row['user_id']}>."
                )

    @discord.ui.button(label="Одобрить", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.finish(interaction, True)

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.finish(interaction, False)


@bot.event
async def on_ready():
    # Register persistent panel and buttons for pending applications after restarts.
    bot.add_view(ApplicationPanelView())
    rows = db.execute(
        "SELECT id FROM applications WHERE status='pending' AND review_message_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        bot.add_view(ReviewView(row["id"]))
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            log.info("Синхронизировано команд для сервера %s: %s", GUILD_ID, [cmd.name for cmd in synced])
        else:
            synced = await bot.tree.sync()
            log.info("Синхронизировано глобальных команд: %s", [cmd.name for cmd in synced])
    except Exception:
        log.exception("Slash command sync failed")
    log.info("Бот запущен как %s", bot.user)


@bot.tree.command(name="setup_resignation", description="Create the resignation application panel")
@app_commands.guild_only()
async def resignation_panel(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not reviewer_allowed(interaction.user):
        return await interaction.response.send_message(
            "Эту панель может создавать только старший состав.",
            ephemeral=True
        )
    embed = discord.Embed(
        title="Расторжение контракта",
        description=(
            "Нажмите кнопку ниже, чтобы открыть форму расторжения контракта "
            "по собственному желанию.\n\n"
            "**Требуемые данные:**\n"
            "• Игровой никнейм\n"
            "• Static ID\n"
            "• Текущее звание\n"
            "• Причина увольнения"
        ),
        color=discord.Color.from_rgb(47, 91, 79)
    )
    embed.set_footer(text="ГИБДД | RMRP «Тверской»")
    await interaction.response.send_message(embed=embed, view=ApplicationPanelView())


bot.run(TOKEN)
