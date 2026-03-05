import os
import re
import io
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ======================
# ENV / CONFIG
# ======================
TOKEN = os.getenv("DISCORD_TOKEN")

PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0"))
CATEGORY_ID = int(os.getenv("CATEGORY_ID", "0"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))

# Opcional, mas altamente recomendado (pra receber transcrição + resumo)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# Opcional: fixa e atualiza o painel sempre na mesma mensagem
PANEL_MESSAGE_ID = int(os.getenv("PANEL_MESSAGE_ID", "0"))

# ======================
# BOT
# ======================
INTENTS = discord.Intents.default()
INTENTS.guilds = True

BOT_NAME = "Central de Denúncias"
THEME_COLOR = discord.Color.red()


def sanitize_channel_name(text: str) -> str:
    text = text.lower().replace(" ", "-")
    text = re.sub(r"[^a-z0-9\-]", "", text)
    text = re.sub(r"\-+", "-", text).strip("-")
    if not text:
        text = "usuario"
    return text[:18]


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def build_transcript_txt(channel: discord.TextChannel) -> discord.File:
    """
    Gera um .txt simples com o histórico do ticket.
    """
    lines = []
    lines.append(f"=== TRANSCRIÇÃO DO TICKET ===")
    lines.append(f"Servidor: {channel.guild.name} ({channel.guild.id})")
    lines.append(f"Canal: #{channel.name} ({channel.id})")
    lines.append(f"Gerado em: {utc_now_str()}")
    lines.append("")

    # Histórico do mais antigo pro mais novo
    async for msg in channel.history(limit=None, oldest_first=True):
        author = f"{msg.author} ({msg.author.id})"
        timestamp = msg.created_at.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        content = msg.content if msg.content else ""

        # Anexos
        if msg.attachments:
            attach_urls = " | ".join(a.url for a in msg.attachments)
            content = (content + "\n" if content else "") + f"[Anexos] {attach_urls}"

        # Embeds (resumo)
        if msg.embeds:
            content = (content + "\n" if content else "") + f"[Embeds] {len(msg.embeds)} embed(s)"

        lines.append(f"[{timestamp}] {author}: {content}")

    data = "\n".join(lines).encode("utf-8")
    fp = io.BytesIO(data)
    return discord.File(fp=fp, filename=f"transcript-{channel.name}.txt")


def panel_embed() -> discord.Embed:
    e = discord.Embed(
        title="📣 Central de Denúncias",
        description=(
            "Abra um ticket privado para enviar sua denúncia.\n\n"
            "**Escolha uma opção:**\n"
            "• **Denúncia identificada**: seu usuário aparece para a moderação.\n"
            "• **Denúncia anônima**: seu nome **não** aparece no texto da denúncia.\n\n"
            "✅ A denúncia será enviada para um canal privado visível apenas para você e a staff."
        ),
        color=THEME_COLOR,
    )
    e.add_field(
        name="🧾 O que enviar",
        value="Assunto + detalhes (datas, nomes, contexto) e links/provas se tiver.",
        inline=False,
    )
    e.set_footer(text="Use com responsabilidade • Denúncias falsas podem gerar punição")
    return e


def ticket_embed(
    anon: bool,
    reporter: discord.Member,
    assunto: str,
    detalhes: str,
    provas: str,
) -> discord.Embed:
    title = "🕵️ Denúncia Anônima" if anon else "🧾 Nova Denúncia"
    e = discord.Embed(
        title=title,
        description="Ticket criado automaticamente. A staff irá avaliar.",
        color=discord.Color.orange() if anon else discord.Color.gold(),
    )

    if anon:
        e.add_field(name="👤 Denunciante", value="(anônimo)", inline=False)
        e.set_footer(text="Modo anônimo: o nome não foi incluído no conteúdo do ticket.")
    else:
        e.add_field(name="👤 Denunciante", value=f"{reporter.mention} (`{reporter.id}`)", inline=False)

    e.add_field(name="🏷️ Assunto", value=assunto[:256], inline=False)
    e.add_field(name="📝 Detalhes", value=(detalhes[:1024] if detalhes else "—"), inline=False)

    provas = (provas or "").strip()
    e.add_field(name="🔗 Provas", value=(provas if provas else "—"), inline=False)

    e.add_field(name="📌 Status", value="⏳ Aguardando staff assumir", inline=False)
    return e


# ======================
# MODAIS
# ======================
class DenunciaModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, anon: bool):
        super().__init__(title="📣 Formulário de Denúncia")
        self.bot = bot
        self.anon = anon

        self.assunto = discord.ui.TextInput(
            label="Assunto (curto e direto)",
            placeholder="Ex.: Assédio / Spam / Golpe / Ofensa",
            max_length=80,
        )
        self.detalhes = discord.ui.TextInput(
            label="Detalhes (o que aconteceu?)",
            placeholder="Explique com datas, nomes e contexto…",
            style=discord.TextStyle.paragraph,
            max_length=1200,
        )
        self.provas = discord.ui.TextInput(
            label="Links/Provas (opcional)",
            placeholder="Links de prints/mensagens/vídeos…",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=500,
        )

        self.add_item(self.assunto)
        self.add_item(self.detalhes)
        self.add_item(self.provas)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Isso funciona apenas em servidor.", ephemeral=True)

        # validações básicas
        if not TOKEN or PANEL_CHANNEL_ID == 0 or CATEGORY_ID == 0 or STAFF_ROLE_ID == 0:
            return await interaction.response.send_message(
                "Configuração incompleta: verifique as variables no Railway.",
                ephemeral=True,
            )

        category = guild.get_channel(CATEGORY_ID)
        staff_role = guild.get_role(STAFF_ROLE_ID)

        if category is None or not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("CATEGORY_ID inválido.", ephemeral=True)
        if staff_role is None:
            return await interaction.response.send_message("STAFF_ROLE_ID inválido.", ephemeral=True)

        reporter = interaction.user
        base = sanitize_channel_name(reporter.name)
        # canal fica rastreável sem expor muito
        channel_name = f"denuncia-{base}-{str(reporter.id)[-4:]}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            reporter: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }

        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason="Novo ticket de denúncia",
        )

        # guardamos metadados no tópico (útil pro log)
        anon_flag = "anon" if self.anon else "id"
        await ticket_channel.edit(topic=f"denuncia:{anon_flag} reporter:{reporter.id} created:{utc_now_str()}")

        embed = ticket_embed(
            anon=self.anon,
            reporter=reporter,
            assunto=str(self.assunto),
            detalhes=str(self.detalhes),
            provas=str(self.provas),
        )

        view = TicketView()

        await ticket_channel.send(content=f"{staff_role.mention}", embed=embed, view=view)

        msg = f"✅ Ticket criado: {ticket_channel.mention}"
        if self.anon:
            msg += "\n🕵️ Você enviou em modo **anônimo** (seu nome não aparece no texto do ticket)."
        await interaction.response.send_message(msg, ephemeral=True)


class CloseTicketModal(discord.ui.Modal, title="🔒 Fechar Ticket"):
    motivo = discord.ui.TextInput(
        label="Motivo do fechamento",
        placeholder="Ex.: Resolvido / Sem provas / Denúncia inválida / Encaminhado",
        style=discord.TextStyle.paragraph,
        max_length=400,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Canal inválido.", ephemeral=True)

        channel: discord.TextChannel = interaction.channel
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Servidor inválido.", ephemeral=True)

        # cria transcript
        transcript_file = await build_transcript_txt(channel)

        # tenta logar
        log_ok = False
        if LOG_CHANNEL_ID != 0:
            log_channel = guild.get_channel(LOG_CHANNEL_ID)
            if isinstance(log_channel, discord.TextChannel):
                close_embed = discord.Embed(
                    title="📦 Ticket Fechado",
                    description=f"Canal: `#{channel.name}` (`{channel.id}`)",
                    color=discord.Color.dark_grey(),
                )
                close_embed.add_field(name="👮 Fechado por", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
                close_embed.add_field(name="🧾 Motivo", value=str(self.motivo)[:1024], inline=False)
                close_embed.add_field(name="🕒 Data", value=utc_now_str(), inline=False)
                if channel.topic:
                    close_embed.add_field(name="ℹ️ Topic", value=channel.topic[:1024], inline=False)

                await log_channel.send(embed=close_embed, file=transcript_file)
                log_ok = True

        await interaction.response.send_message(
            ("✅ Ticket fechado. Transcript enviado para logs." if log_ok else "✅ Ticket fechado. (LOG_CHANNEL_ID não configurado)"),
            ephemeral=True,
        )

        # dá um tempinho pro Discord registrar a resposta e apaga
        await channel.delete(reason=f"Ticket fechado por {interaction.user} | motivo: {self.motivo}")


# ======================
# VIEWS (BOTÕES)
# ======================
class PanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="📣 Denúncia (identificada)",
        style=discord.ButtonStyle.danger,
        custom_id="denuncia:open:identified",
    )
    async def open_identified(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DenunciaModal(self.bot, anon=False))

    @discord.ui.button(
        label="🕵️ Denúncia (anônima)",
        style=discord.ButtonStyle.secondary,
        custom_id="denuncia:open:anon",
    )
    async def open_anon(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DenunciaModal(self.bot, anon=True))


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ Assumir",
        style=discord.ButtonStyle.success,
        custom_id="denuncia:claim",
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Servidor inválido.", ephemeral=True)

        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role is None:
            return await interaction.response.send_message("STAFF_ROLE_ID inválido.", ephemeral=True)

        # só staff pode assumir
        if not isinstance(interaction.user, discord.Member) or staff_role not in interaction.user.roles:
            return await interaction.response.send_message("Apenas a staff pode assumir.", ephemeral=True)

        # atualiza embed da mensagem que contém os botões
        if interaction.message and interaction.message.embeds:
            e = interaction.message.embeds[0]
            # recria embed com as fields (discord.Embed de message é "read-only", então clonamos)
            new = discord.Embed(title=e.title, description=e.description, color=e.color)
            for f in e.fields:
                if f.name == "📌 Status":
                    continue
                new.add_field(name=f.name, value=f.value, inline=f.inline)

            new.add_field(name="📌 Status", value=f"🧑‍⚖️ Assumido por {interaction.user.mention}", inline=False)
            if e.footer and e.footer.text:
                new.set_footer(text=e.footer.text)

            await interaction.message.edit(embed=new, view=self)

        await interaction.response.send_message(f"✅ Você assumiu este ticket.", ephemeral=True)

    @discord.ui.button(
        label="🧾 Transcrição",
        style=discord.ButtonStyle.primary,
        custom_id="denuncia:transcript",
    )
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Canal inválido.", ephemeral=True)

        channel: discord.TextChannel = interaction.channel
        file = await build_transcript_txt(channel)
        await interaction.response.send_message("🧾 Aqui está a transcrição do ticket:", file=file, ephemeral=True)

    @discord.ui.button(
        label="🔒 Fechar",
        style=discord.ButtonStyle.secondary,
        custom_id="denuncia:close",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Servidor inválido.", ephemeral=True)

        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role is None:
            return await interaction.response.send_message("STAFF_ROLE_ID inválido.", ephemeral=True)

        # só staff pode fechar
        if not isinstance(interaction.user, discord.Member) or staff_role not in interaction.user.roles:
            return await interaction.response.send_message("Apenas a staff pode fechar.", ephemeral=True)

        await interaction.response.send_modal(CloseTicketModal())


# ======================
# STARTUP / PAINEL
# ======================
async def ensure_panel(bot: commands.Bot):
    channel = bot.get_channel(PANEL_CHANNEL_ID)
    if channel is None or not isinstance(channel, discord.TextChannel):
        print("⚠️ PANEL_CHANNEL_ID inválido ou canal não encontrado.")
        return

    embed = panel_embed()
    view = PanelView(bot)

    global PANEL_MESSAGE_ID

    if PANEL_MESSAGE_ID != 0:
        try:
            msg = await channel.fetch_message(PANEL_MESSAGE_ID)
            await msg.edit(embed=embed, view=view)
            print("✅ Painel atualizado (PANEL_MESSAGE_ID).")
            return
        except Exception:
            pass

    msg = await channel.send(embed=embed, view=view)
    print(f"📌 Painel criado. Salve esta variável no Railway: PANEL_MESSAGE_ID={msg.id}")


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)

    async def setup_hook(self):
        # Views persistentes (botões continuam funcionando após restart)
        self.add_view(PanelView(self))
        self.add_view(TicketView())

    async def on_ready(self):
        print(f"✅ Logado como {self.user} (ID: {self.user.id})")
        await ensure_panel(self)


def main():
    if not TOKEN:
        raise RuntimeError("Defina DISCORD_TOKEN nas variables (Railway).")
    bot = Bot()
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
