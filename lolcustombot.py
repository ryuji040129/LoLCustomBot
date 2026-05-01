import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import random

TOKEN = ""

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="/", intents=intents)


conn = sqlite3.connect("lolelo.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS players (
    user_id TEXT PRIMARY KEY,
    name TEXT,
    rating INTEGER,
    win_streak INTEGER
)
""")
conn.commit()


def get_rating(user_id):
    cursor.execute("SELECT rating FROM players WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def set_rating(user_id, rating):
    cursor.execute(
        "INSERT INTO players (user_id, name, rating, win_streak) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET rating=?",
        (user_id, "", rating, 0, rating)
    )
    conn.commit()

def get_streak(user_id):
    cursor.execute("SELECT win_streak FROM players WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else 0

def set_streak(user_id, streak):
    cursor.execute("UPDATE players SET win_streak=? WHERE user_id=?", (streak, user_id))
    conn.commit()

def reset_all_ratings():
    cursor.execute("UPDATE players SET rating = 1000, win_streak = 0")
    conn.commit()
# =========================
# ELO
# =========================
def update_elo(player, opponent_avg, result, streak):
    K = 32
    expected = 1 / (1 + 10 ** ((opponent_avg - player) / 400))
    base = K * (result - expected)
    bonus = min(streak * 2, 10) if result == 1 else 0
    return int(player + base + bonus)

# =========================
# チーム生成
# =========================
def fast_split(players, trials=500):
    best, best_diff = None, float("inf")
    for _ in range(trials):
        random.shuffle(players)
        half = len(players)//2
        t1 = players[:half]
        t2 = players[half:]
        diff = abs(sum(p["score"] for p in t1) - sum(p["score"] for p in t2))
        if diff < best_diff:
            best_diff = diff
            best = (t1[:], t2[:])
    return best

def generate_teams(members):
    players = []

    for m in members:
        uid = str(m.id)
        rating = get_rating(uid)

        if rating is None:
            cursor.execute(
                "INSERT INTO players VALUES (?, ?, ?, ?)",
                (uid, m.name, 1000, 0)
            )
            conn.commit()
            rating = 1000

        cursor.execute("UPDATE players SET name=? WHERE user_id=?", (m.name, uid))
        conn.commit()

        players.append({"id": uid, "name": m.name, "score": rating})

    if len(players) < 2:
        return None, None, None

    extra = None
    if len(players) % 2 != 0:
        extra = random.choice(players)
        players.remove(extra)

    t1, t2 = fast_split(players)
    return t1, t2, extra


# =========================
# 状態
# =========================
current_match = {}
auto_stop = {}
autoplay = {}


def create_embed(t1, t2, extra):
    embed = discord.Embed(
        title="⚔️ チーム分け",
        color=discord.Color.blue()
    )

    team1 = "\n".join([f"<@{p['id']}>" for p in t1])
    team2 = "\n".join([f"<@{p['id']}>" for p in t2])
    
    embed.add_field(name="🟦 Team1", value=team1, inline=True)
    embed.add_field(name="🟥 Team2", value=team2, inline=True)

    if extra:
        embed.add_field(name="👀 観戦", value=f"<@{extra['id']}>", inline=False)

    return embed

# =========================
# ボタン
# =========================
class ResultView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    @discord.ui.button(label="Team1 勝ち", style=discord.ButtonStyle.green)
    async def t1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process(interaction, 1)

    @discord.ui.button(label="Team2 勝ち", style=discord.ButtonStyle.red)
    async def t2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process(interaction, 2)
        
    @discord.ui.button(label="⛔ オート終了", style=discord.ButtonStyle.gray)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        auto_stop[self.guild_id] = True

        await interaction.response.send_message("⛔ オートプレイを停止しました",ephemeral=True)
    async def process(self, interaction, winner):
        if self.guild_id not in current_match:
            await interaction.response.send_message("試合なし", ephemeral=True)
            return

        match = current_match[self.guild_id]
        t1, t2 = match["team1"], match["team2"]

        avg1 = int(sum(p["score"] for p in t1)/len(t1))
        avg2 = int(sum(p["score"] for p in t2)/len(t2))

#        value=f"{team1}\n平均: {avg1}"
#        value=f"{team2}\n平均: {avg2}"

        if winner == 1:
            winners, losers, w_avg, l_avg = t1, t2, avg2, avg1
        else:
            winners, losers, w_avg, l_avg = t2, t1, avg1, avg2

        for p in winners:
            s = get_streak(p["id"]) + 1
            set_streak(p["id"], s)
            set_rating(p["id"], update_elo(p["score"], w_avg, 1, s))

        for p in losers:
            set_streak(p["id"], 0)
            set_rating(p["id"], update_elo(p["score"], l_avg, 0, 0))

        del current_match[self.guild_id]


        await interaction.response.edit_message(content="✅ 更新完了",view=None)

        # 常に次試合
        vc = interaction.user.voice.channel if interaction.user.voice else None

        # 自動連戦
        if autoplay.get(self.guild_id, False) and not auto_stop.get(self.guild_id, False):
            await self.next_match(interaction)
        else:
            await interaction.followup.send("次の試合を開始できません")
        #  自動連戦
       # if autoplay.get(self.guild_id, False):

    async def next_match(self, interaction):
        match = current_match.get(self.guild_id)

        if not match:
            await interaction.followup.send("試合データがありません")
            return

        members = match["members"]  # ←保存してたやつ使う

        t1, t2, extra = generate_teams(members)

        if not t1:
            await interaction.followup.send("人数不足")
            return

        current_match[self.guild_id] = {
            "team1": t1,
            "team2": t2,
            "members": members
        }

        embed = create_embed(t1, t2, extra)

        await interaction.followup.send(
        embed=embed,
        view=ResultView(self.guild_id)
    )

# =========================
# 起動
# =========================
@bot.event
async def on_ready():
    guild = discord.Object(id=696554133766930483)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    print("ready")

# =========================
# /team
# =========================
@bot.tree.command(name="team")
@app_commands.describe(exclude="除外ユーザー")
async def team(interaction: discord.Interaction, exclude: discord.Member = None):

    if not interaction.user.voice:
        await interaction.response.send_message("VC入って")
        return

    members = [m for m in interaction.user.voice.channel.members if not m.bot]

    if exclude:
        members = [m for m in members if m.id != exclude.id]

    t1, t2, extra = generate_teams(members)

    if not t1:
        await interaction.response.send_message("人数不足")
        return

    current_match[interaction.guild.id] = {
        "team1": t1,
        "team2": t2,
        "members": members
}
    embed = create_embed(t1, t2, extra)

    await interaction.response.send_message(
       embed=embed,
       view=ResultView(interaction.guild.id)
    )
    auto_stop[interaction.guild.id] = False
#=========================
#/autoplay
# =========================
@bot.tree.command(name="autoplay")
@app_commands.describe(mode="on / off")
async def autoplay_cmd(interaction: discord.Interaction, mode: str):

    if mode not in ["on", "off"]:
        await interaction.response.send_message("on / off で指定してね")
        return

    autoplay[interaction.guild.id] = (mode == "on")
    await interaction.response.send_message(f"連戦モード: {mode}")
    
@bot.tree.command(name="reset_rating")
async def reset_rating(interaction: discord.Interaction):

    reset_all_ratings()

    await interaction.response.send_message("✅ 全プレイヤーのレートをリセットしました")
        
bot.run(TOKEN)