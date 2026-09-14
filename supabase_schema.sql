-- Durable long-term archive of every Instagram DM. Clearly prefixed and
-- fully separate from the existing ERP schema - this never touches or reads
-- any other table in this project.
create table if not exists ig_bot_messages (
    id          bigint generated always as identity primary key,
    convo       text not null,
    role        text not null check (role in ('user', 'assistant')),
    content     text not null,
    media_type  text,
    media_path  text,
    referral    text,
    created_at  timestamptz not null default now()
);

create index if not exists ig_bot_messages_convo_idx
    on ig_bot_messages (convo, created_at);

-- The mid of the message this one quoted, when the customer tapped "reply"
-- on a specific earlier message instead of sending fresh.
alter table ig_bot_messages add column if not exists reply_to_mid text;
