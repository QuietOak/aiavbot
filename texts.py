# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
texts.py - Everything members see, in one place.

Edit wording, collab types and response types here. No other file needs to change.
Keep the Dreamers AI Hub tone: warm, relaxed, creative, never corporate.
Discord limits worth knowing: button labels <= 80 chars, dropdown option labels <= 100,
pop-up titles and field labels <= 45.
"""

# ----------------------------------------------------------------- public reply
# Shown under the member's post. {name} = their server display name.
PUBLIC_REPLY = ("🎵 Thanks so much for sharing with us, **{name}**! "
                "Now let's make sure your work gets all the attention it deserves!")
OPEN_BUTTON_LABEL = "Enhance Sharing"
OPEN_BUTTON_EMOJI = "🚀"
# Link buttons on the public reply that open the song so anyone can like it there.
LIKE_BUTTON = "❤️ Like on {site}"
LIKE_BUTTON_N = "❤️ Like #{n} on {site}"     # second song from the same site in one post
# After the reply timeout (or "Just sharing"), the reply shrinks to this but keeps its buttons.
PUBLIC_REPLY_COMPACT = "🎵 Shared by **{name}**. Enjoying it? Show some love! ❤️"

# Private note when someone other than the poster clicks the button.
NOT_YOUR_POST = (
    "These options are for the person who shared this song. "
    "Share your own track here and you'll get some too! 🎶"
)
SHARE_GONE = "This share has expired, but thanks for checking it out! 🎶"

# ------------------------------------------------------------------ private panel
# One is picked per post (stays the same if the panel is reopened).
# {title} = song title (falls back to "your song").
THANK_YOU = [
    "Thanks for sharing **{title}**! We love hearing what Dreamers are making.",
    "**{title}** just landed. Thanks for bringing it here!",
    "Thanks for sharing **{title}** with Dreamers! 🎶",
    "New music! Thanks for sharing **{title}**. It's always great to hear what you're creating.",
    "Thanks for dropping **{title}** in! Every track adds to the mix here.",
]
PANEL_FOOTER = "Each one takes a few seconds and helps your music get found. 💜"
# The pitch shown just above the buttons. The theme line only appears while a theme is active.
PANEL_PITCH_TITLE = "✨ Make it stand out"
PANEL_PITCH = [
    ("line", "💬 **Label your music** in your own words"),
    ("thread", "🧵 **Start a conversation** with listeners"),
    ("collab", "🤝 **Bring it to the next level** with a collab"),
    ("theme", "🏷️ **Join a theme!** Share it to {themes}"),
]
PANEL_LOADING = "*Song details are still loading. Everything still works.*"
PANEL_FROM = "From"   # shown for non-Suno links, e.g. "YouTube · Channel name"
PICK_SONG_PLACEHOLDER = "Which song are these options for?"

BTN_LINE = "💬 Label it in your words"
BTN_LINE_EDIT = "✏️ Edit your line"
BTN_COLLAB = "🤝 Post to Collab Requests"
BTN_COLLAB_DONE = "🤝 View your request"
BTN_THREAD = "🧵 Start a conversation"
BTN_THREAD_DONE = "🧵 Go to thread"
BTN_DONE = "👍 Just sharing, thanks!"
# Theme button: shown only while a theme is active. {emoji} {name} = the theme.
BTN_THEME_ONE = "{emoji} Join {name}"
BTN_THEME_MANY = "🏷️ Join a theme"
BTN_THEME_DONE = "{emoji} See it in {name}"

JUST_SHARING_REPLY = (
    "Enjoy! 💛 Thanks again for sharing. If you ever want a collaborator for a track, "
    "AIAV Club is always open."
)

# ------------------------------------------------------------ "Add a line" pop-up
LINE_MODAL_TITLE = "Add a line about your song"
LINE_LABEL = "What's the story behind it?"
LINE_DESCRIPTION = "The idea, mood, a favourite moment, anything. One line is plenty."
LINE_PLACEHOLDER = "Wrote this after a 3am thunderstorm..."
LINE_MAX = 300
LINE_DONE = "Added! Your line now shows under your post. 💬"

# Public reply additions
REPLY_LINE = "💬 **{name}:** “{line}”"
REPLY_THEME = "{label} → {url}"
REPLY_COLLAB = "🤝 Open for collab → {url}"
REPLY_THREAD = "🧵 Chat about it → {url}"

# ------------------------------------------------------ "Share to a theme" pop-up
# Themes are set by mods with /aiav season add. Each has a channel, thread or forum.
THEME_MODAL_TITLE = "Share to {name}"          # one theme active
THEME_MODAL_TITLE_MANY = "Share to a theme"     # several active
THEME_PICK_LABEL = "Which theme?"
THEME_NOTE_LABEL = "How does it fit the theme? (optional)"
THEME_NOTE_DESCRIPTION = "A few words for people browsing the theme."
THEME_NOTE_PLACEHOLDER = "e.g. the bridge sounds like a haunted music box"
THEME_NOTE_MAX = 300
THEME_HEADER = "{label} · shared by {mention}"
THEME_POSTED = "Shared to {label}: {url}"
THEME_ALL_DONE = "You've already shared this song to every current theme. 🎉"
THEME_FAILED = ("I couldn't post in that theme's channel. A moderator may need to check my permissions "
                "(or the forum may require tags).")

# --------------------------------------------------------------- Collab pop-up
COLLAB_MODAL_TITLE = "Post to Collab Requests"
COLLAB_TYPES_LABEL = "What kind of collab?"
COLLAB_TYPES_DESCRIPTION = "Pick as many as you like."
COLLAB_NOTE_LABEL = "Anything else? (optional)"
COLLAB_NOTE_PLACEHOLDER = "e.g. dark fantasy vibe, would love an animated loop for the chorus"
COLLAB_NOTE_MAX = 500

# (value, label, emoji, short description)
COLLAB_TYPES = [
    ("art", "Cover art / image", "🎨", "Artwork for the song"),
    ("video", "Animation / video / visualizer", "🎬", "Moving pictures for the music"),
    ("character", "Character", "🧝", "A character design or a character this is the theme for"),
    ("musician", "Another musician", "🎤", "Vocals, lyrics, remix, arrangement"),
    ("story", "Story / worldbuilding", "📜", "A scene, story or world around the song"),
    ("open", "Open to anything", "✨", "Surprise me"),
]

COLLAB_CARD_HEADER = "🤝 **New collab request from {mention}**"
COLLAB_LOOKING_FOR = "Looking for"
COLLAB_NOTE_FIELD = "Notes"
COLLAB_STYLE_FIELD = "Style"
COLLAB_LISTEN = "🎧 Listen on {site}"   # {site} = Suno, YouTube, Spotify, ...
COLLAB_ORIGINAL = "💬 Original post"
COLLAB_INTERESTED = "🙋 I'm interested"
COLLAB_POSTED = "Posted! Here's your request: {url}\nIt has its own thread, and anyone who clicks **I'm interested** gets pinged into it with you. 🤝"
COLLAB_THREAD_NAME = "🤝 {title}"
COLLAB_THREAD_INTRO = ("🤝 This is the collab thread for **{title}** by {mention}.\n"
                       "Interested? Click **🙋 I'm interested** on the request, or just say hi here!")
COLLAB_IDEA_TITLE = "{name}'s collab idea"
CHARACTER_FALLBACK_TITLE = "{name}'s character"
COLLAB_MODAL_TITLE_CHARACTER = "Find collaborators for your character"
COLLAB_TYPES_LABEL_CHARACTER = "What would you love for this character?"
CHARACTER_ABOUT_FIELD = "About the character"
CHARACTER_TAGS_FIELD = "Tags"
CHARACTER_CREATOR_FIELD = "Card creator"
CHARACTER_ADULT_NOTE = "🔞 Tagged 18+. Details are on the card itself."
CHARACTER_OPEN = "⬇️ Open on {site}"          # link button on character cards

# Collab types offered when the request is for a character. (value, label, emoji, short description)
CHARACTER_COLLAB_TYPES = [
    ("theme", "Theme song", "🎵", "Music that captures this character"),
    ("art", "New art / portrait", "🎨", "Fresh artwork of the character"),
    ("video", "Animation / video", "🎬", "Bring them to life on screen"),
    ("voice", "Voice / TTS", "🎙️", "A voice for the character"),
    ("story", "Story / scene", "📜", "Write them into a story"),
    ("rp", "Roleplay / co-write", "🎭", "Play or write a scene together"),
    ("world", "Worldbuilding / lorebook", "🗺️", "Expand their world"),
    ("open", "Open to anything", "✨", "Surprise me"),
]
COLLAB_IDEA_FIELD = "The idea"                     # quote of their post, on cards from the collab channel
COLLAB_THREAD_ORIGINAL = "📌 Original post: {url}"
COLLAB_NO_CHANNEL = "The collab requests channel isn't set up yet. Please let a moderator know!"

INTEREST_SELF = "That's your own request. Hang tight, someone will come along! 🙂"
INTEREST_AGAIN = "You already raised your hand on this one. Here's the thread: {url}"
INTEREST_THREAD_NAME = "🤝 {title}"
INTEREST_PING = "🙋 {interested} is interested in collaborating with {creator} on **{title}**! Take it from here. 🎶"
INTEREST_DONE = "Nice! I opened a thread with {creator}: {url}"

# --------------------------------------------------------------- Thread pop-up
THREAD_MODAL_TITLE = "Start a thread for your song"
RESPONSE_LABEL = "What kind of response would you like?"
RESPONSE_DESCRIPTION = "Helps listeners know how to reply."
# (value, label, emoji)
RESPONSE_TYPES = [
    ("enjoy", "Just enjoy it", "🎧"),
    ("reactions", "Reactions welcome", "😊"),
    ("feedback", "Friendly feedback", "💬"),
    ("critique", "Critique welcome", "🔍"),
    ("collab", "Looking for collaborators", "🤝"),
]
THREAD_NOTE_LABEL = "Opening note (optional)"
THREAD_NOTE_PLACEHOLDER = "Anything you'd like listeners to know"
THREAD_NOTE_MAX = 1000

THREAD_NAME = "🎵 {title}"
THREAD_FALLBACK_TITLE = "{name}'s song"
THREAD_INTRO = "Welcome to the thread for **{title}** by {mention}! 🎶"
THREAD_WANTS = "**{name} would love:** {wants}"
THREAD_PROMPTS = [
    "What inspired it?",
    "What's your favourite moment in it?",
    "What's next for this track?",
]
THREAD_PROMPTS_HEADER = "Some easy ways to jump in:"
THREAD_DONE = "Thread started: {url} 🧵"
THREAD_FAILED = "I couldn't start a thread there. A moderator may need to check my permissions."
COLLAB_FAILED = "I couldn't post in the collab requests channel. A moderator may need to check my permissions."

# ----------------------------------------------------------------------- misc
GENERIC_ERROR = "Something went wrong on my end. Please try again in a moment."


# ================================================================ collab channel
# When someone posts directly in the collab requests channel (not in a thread).
COLLAB_PROMPT = "🤝 Starting a collab, **{name}**?"
COLLAB_PROMPT_YES = "Yes, post a collab request"
COLLAB_PROMPT_NO = "No, not a request"
COLLAB_NOT_YOURS = "Only the person who posted this can turn it into a collab request."
COLLAB_POSTED_HERE = "Your collab request is up, with its own thread: {url} 🤝"
# Private, friendly reminder when someone answers "No" in the collab channel.
# When the post in the collab channel is a character (Stoop/Chub/... link or a PNG/JSON card file)
COLLAB_PROMPT_CHARACTER = "🎭 Looks like a character, **{name}**! Want to find collaborators for them?"
COLLAB_PROMPT_CHARACTER_YES = "Yes, find collaborators"
COLLAB_REMINDER = ("Thanks for posting, {name}! 💛 Just a friendly heads-up: {channel} is kept for collab "
                   "requests so they're easy to browse. For chatting and other shares, a thread here or "
                   "{lounge} is the perfect spot.")
LOUNGE_FALLBACK = "the AIAV Club lounge"

# ======================================================================= gallery
# When someone posts in the gallery channel (not in a thread).
GALLERY_PROMPT = "✨ Is this a collab result, **{name}**?"
GALLERY_PROMPT_YES = "🎉 Yes, it's a collab!"
GALLERY_PROMPT_NO = "No, not a collab"
GALLERY_NOT_YOURS = "Only the person who posted this can present it as a collab."
# Private, friendly reminder when someone answers "No" in the gallery.
GALLERY_REMINDER = ("Thanks for sharing, {name}! 💛 Just a friendly heads-up: {channel} is for finished "
                    "AIAV collab results. For other work, a thread here or {lounge} is the perfect spot{music}.")
GALLERY_REMINDER_MUSIC = ", and music is always welcome in {music}"

GALLERY_MODAL_TITLE = "Present your collab"
GALLERY_TITLE_LABEL = "Title (optional)"
GALLERY_TITLE_PLACEHOLDER = "What's it called?"
GALLERY_MEMBERS_LABEL = "Who did you collab with?"
GALLERY_MEMBERS_DESCRIPTION = "Pick people from this server (optional if they're not here)."
GALLERY_OTHERS_LABEL = "Anyone else? (optional)"
GALLERY_OTHERS_DESCRIPTION = "Names or handles of collaborators who aren't on this server."
GALLERY_OTHERS_PLACEHOLDER = "e.g. Nova (@nova on Suno), my friend Sam"
GALLERY_NOTE_LABEL = "About the piece (optional)"
GALLERY_NOTE_PLACEHOLDER = "How it came together, who did what, what you love about it"

GALLERY_DEFAULT_TITLE = "A new AIAV collab"
GALLERY_HEADER = "🎉 **New AIAV Club collaboration!**"
GALLERY_CONGRATS = "Congratulations to everyone who brought this to life! ✨"
GALLERY_CREATED_BY = "Created by"
GALLERY_ABOUT = "About this piece"
GALLERY_SPOILER_NOTE = "🔒 The image is spoilered. Click it below to view."
GALLERY_VIDEO_NOTE = "🎬 See the original post above for the full piece."
GALLERY_FOOTER = "AIAV Club · music, visuals & collaboration"
GALLERY_THREAD_NAME = "💬 {title}"
GALLERY_THREAD_INTRO = ("🎉 Congratulations {people} on **{title}**!\n"
                        "Leave your comments, kudos and questions for the creators here. 💬")
GALLERY_POSTED = "Your collab is presented, with a comment thread: {url} 🎉"

# ================================================================ nightly update
# Posted in the lounge at midnight (local time of the PC running the bot).
NIGHTLY_TITLE = "🌙 AIAV Club nightly update"
UPDATE_TITLE = "📊 AIAV Club update"
NIGHTLY_INTRO = "Here's what's been happening around AIAV Club. Thanks to everyone sharing and collaborating! 💜"
NIGHTLY_PERIODS = [(1, "Last day"), (7, "Last 7 days"), (30, "Last 30 days")]
# (action logged by the bot, label). Order = order shown.
STAT_LABELS = [
    ("shared", "shares"),
    ("open_panel", "panels opened"),
    ("line", "lines"),
    ("thread", "threads"),
    ("collab", "collab requests"),
    ("interested", "'interested'"),
    ("theme", "theme shares"),
    ("gallery_collab", "gallery collabs"),
    ("reactions", "reactions"),
    ("milestone", "milestones"),
    ("just_sharing", "just sharing"),
]


# ============================================================= reaction milestones
# When a post with a link, image or file (or one of the bot's song/collab/gallery cards)
# reaches these reaction totals, the bot replies to it in the same channel.
# {author} = the creator (shown, not pinged), {count} = reactions, {link} = jump link to the post.
MILESTONES = [3, 10, 20, 30, 50]
MILESTONE_MESSAGES = {
    3: "🔥 Ooh, {author}'s post is catching on: **{count} reactions** already! {link}",
    10: "✨ Wow, {link} just hit **{count} reactions**! Go {author}!",
    20: "🚀 **{count} reactions** on {author}'s post! People are loving it. {link}",
    30: "🌟 **{count} reactions!** {author}'s post is a Dreamers favourite. {link}",
    50: "🏆 **{count} reactions!** Legendary. Huge congratulations, {author}! {link}",
}
MILESTONE_DEFAULT = "🎉 {link} has **{count} reactions**! Congratulations {author}!"
