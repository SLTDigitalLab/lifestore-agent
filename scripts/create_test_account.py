"""Provision the one private tester and optionally recover pre-account conversations."""
import argparse
import secrets
from datetime import datetime, timedelta
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session
from app.api.auth import account_engine, hash_password
from app.db.models import UserAccount, Conversation, ChatTurn, utcnow


def history_turns(messages):
    turns = []
    for message in messages:
        if message.type == 'human':
            turns.append([message.text, None])
        elif message.type == 'ai' and not message.tool_calls and message.text and turns:
            turns[-1][1] = ((turns[-1][1] + '\n\n') if turns[-1][1] else '') + message.text
    return turns


def recover_legacy(user_id):
    from app.graph.checkpoints import get_checkpointer
    engine = account_engine()
    with engine.connect() as connection:
        if not connection.scalar(text("SELECT to_regclass('lifestore_browsing_checkpoints.checkpoints')")):
            return 0
        ids = list(connection.scalars(text("SELECT DISTINCT thread_id FROM lifestore_browsing_checkpoints.checkpoints WHERE checkpoint_ns = ''")))
    saver = get_checkpointer('browsing')
    recovered = 0
    for thread_id in ids:
        try:
            UUID(thread_id)
        except ValueError:
            continue
        checkpoint = saver.get_tuple({'configurable': {'thread_id': thread_id}})
        if not checkpoint:
            continue
        turns = history_turns(checkpoint.checkpoint.get('channel_values', {}).get('messages', []))
        if not turns:
            continue
        timestamp = datetime.fromisoformat(checkpoint.checkpoint['ts'])
        with Session(engine) as db, db.begin():
            if db.get(Conversation, thread_id):
                continue
            db.add(Conversation(id=thread_id, user_id=user_id, title=turns[0][0][:70],
                                created_at=timestamp, updated_at=timestamp))
            db.flush()
            for i, (question, reply) in enumerate(turns):
                db.add(ChatTurn(id=str(uuid5(NAMESPACE_URL, 'legacy:' + thread_id + ':' + str(i))),
                    conversation_id=thread_id, user_text=question,
                    reply=reply or 'No reply was saved for this message.',
                    created_at=timestamp + timedelta(microseconds=i)))
            recovered += 1
    return recovered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--import-legacy', action='store_true')
    args = parser.parse_args()
    password = None
    with Session(account_engine()) as db, db.begin():
        count = db.scalar(select(func.count()).select_from(UserAccount))
        user = db.scalar(select(UserAccount).where(UserAccount.username == 'tester'))
        if count and (not user or count != 1):
            raise SystemExit('Refusing to provision/import into a multi-account database')
        if not user:
            password = secrets.token_urlsafe(18)
            user = UserAccount(id=str(uuid4()), username='tester', name='LifeStore Tester',
                               password_hash=hash_password(password))
            db.add(user)
            db.flush()
        user_id = user.id
    if args.import_legacy:
        print('Recovered conversations:', recover_legacy(user_id))
    print('Username: tester')
    if password:
        print('Password:', password)
    else:
        print('Existing password retained')


if __name__ == '__main__':
    main()
