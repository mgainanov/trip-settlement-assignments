"""Evidence-based settlement CLI. No answer keys, participant names or totals baked in."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

D = Decimal
AMOUNT = r'[−\-]?\d[\d \u00a0\u202f]*(?:[.,]\d{1,2})?'
CURRENCY = r'(?:RUB|AED|CNY|GEL|₽|₾|юан(?:ей|ь|я)|лари)'
SYMBOLS = {'₽': 'RUB', '₾': 'GEL', 'юаней': 'CNY', 'юань': 'CNY', 'юаня': 'CNY', 'лари': 'GEL'}


def decimal(value):
    return D(re.sub(r'[ \u00a0\u202f]', '', str(value)).replace(',', '.').replace('−', '-'))


def cents(value):
    return int((D(value) * 100).quantize(D(1), rounding=ROUND_HALF_UP))


def rub(value):
    return format(D(value) / 100, '.2f')


def parse_amount(text):
    matches = re.findall(rf'({AMOUNT})\s*({CURRENCY})', text, re.I)
    return [(decimal(a), SYMBOLS.get(c.lower(), c.upper())) for a, c in matches]


def date_iso(value, year=None):
    match = re.search(r'\d{4}-\d\d-\d\d', value)
    if match:
        return match[0]
    match = re.search(r'(\d{2})\.(\d{2})(?:\.(\d{4}))?', value)
    if not match or not (match[3] or year):
        raise ValueError(f'Дата без определённого года: {value}')
    return f'{match[3] or year}-{match[2]}-{match[1]}'


def fields(text):
    return dict(re.findall(r'^- ([^:\n]+):\s*(.+)$', text, re.M))


@dataclass
class Message:
    id: str
    date: str
    author: str
    text: str
    source: str


@dataclass
class Receipt:
    id: str
    date: str
    payer: str
    amount: Decimal
    currency: str
    merchant: str
    description: str
    participants: list[str]
    source: str
    weights: dict[str, Decimal] | None = None
    parent: str | None = None
    evidence: list[str] = field(default_factory=list)


@dataclass
class Dataset:
    root: Path
    people: list[str] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    rates: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    rate_sources: dict[tuple[str, str], str] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ocr: dict[str, str] = field(default_factory=dict)
    confirmed_transfers: list[dict] = field(default_factory=list)
    pending_allocations: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)

    def issue(self, kind, text, sources, amount=None, state='open'):
        self.issues.append(dict(kind=kind, text=text, sources=list(dict.fromkeys(sources)), amount_rub=amount, state=state))

    def rate(self, date, currency):
        if currency == 'RUB':
            return D(1)
        if (date, currency) not in self.rates:
            raise ValueError(f'Нет курса {currency} на дату {date}; другой день не подставляется')
        return self.rates[date, currency]


def link_message(path, id, line=None):
    return str(path.resolve()) + (f'#L{line}' if line else f'#{id.lower()}')


def participants_from_chat(text, authors):
    match = re.search(r'Участники:\s*([^\n.]+)', text)
    if match:
        return [n.strip() for n in match[1].split(',')]
    return list(dict.fromkeys(authors))


def read_messages(path, year):
    text = path.read_text()
    result = []
    if re.search(r'^### M\d+', text, re.M):
        for id, body in re.findall(r'^### (M\d+)\s*\n(.*?)(?=^### M\d+\s*$|\Z)', text, re.M | re.S):
            meta = re.search(r'\*\*([^*]+?) — ([^:*]+):\*\*\s*(.*)', body, re.S)
            if not meta:
                raise ValueError(f'Не удалось разобрать сообщение {id}')
            result.append(Message(id, date_iso(meta[1], year), meta[2], meta[3].strip(), link_message(path, id)))
    else:
        for line, row in enumerate(text.splitlines(), 1):
            values = [v.strip() for v in row.strip('|').split('|')]
            if len(values) >= 4 and re.fullmatch(r'M\d+', values[0]):
                result.append(Message(values[0], date_iso(values[1], year), values[2], '|'.join(values[3:]), link_message(path, values[0], line)))
    return result


def name_in_phrase(name, text):
    stem = name[:-1] if name.endswith(('а', 'я')) else name
    return bool(re.search(rf'\b{re.escape(stem)}[а-яё]*\b', text, re.I))


def infer_participants(ds, text, related):
    combined = text + ' ' + ' '.join(m.text for m in related)
    if 'на всех' in combined or '5 персон' in combined or '5 чел' in combined or 'пятер' in combined:
        people = ds.people.copy()
    else:
        people = ds.people.copy()
        ds.notes.append('Равное распределение общего чека без индивидуальных позиций — правило импорта; состав уточняется по сообщениям об отсутствии.')
    for phrase in re.findall(r'(?:без\s+[^.;\n)]+|[А-ЯЁ][а-яё]+\s+не было)', combined):
        for name in ds.people:
            if name_in_phrase(name, phrase) and name in people:
                people.remove(name)
    return people


def read_rates(ds):
    path = ds.root / 'rates.csv'
    if path.exists():
        with path.open() as f:
            for row in csv.DictReader(f):
                key = (row['date'], row['currency'])
                ds.rates[key] = decimal(row['rub_per_unit'])
                ds.rate_sources[key] = next((m.source for m in ds.messages if m.id == row.get('source')), str(path.resolve()))
    path = ds.root / 'rates.md'
    if path.exists():
        text = path.read_text()
        currency = next((c for c in ['AED', 'CNY', 'GEL'] if c in text), None)
        if currency:
            for date, rate in re.findall(r'\|\s*(\d{4}-\d\d-\d\d)\s*\|\s*([\d.,]+)\s*\|', text):
                ds.rates[date, currency] = decimal(rate)
                ds.rate_sources[date, currency] = str(path.resolve())
    active_currency = None
    for message in ds.messages:
        text = message.text
        if not re.search(r'курс', text, re.I):
            continue
        if re.search(r'юан', text, re.I):
            active_currency = 'CNY'
        elif re.search(r'лари', text, re.I):
            active_currency = 'GEL'
        elif 'AED' in text:
            active_currency = 'AED'
        rates = re.findall(r'\b(\d+[.,]\d+)\b', text)
        if active_currency and len(rates) == 1:
            key = message.date, active_currency
            proposed = decimal(rates[0])
            if key in ds.rates and ds.rates[key] != proposed:
                ds.issue('fx_conflict', 'Таблица и сообщение называют разные курсы; расчёт этой даты требует проверки.', [message.source, ds.rate_sources[key]])
                del ds.rates[key]
            else:
                ds.rates[key] = proposed
                ds.rate_sources[key] = message.source


def import_json(ds, path):
    # Only primary evidence fields are consumed. 'disputed', facts, reports ignored.
    data = json.loads(path.read_text(), parse_float=D)
    ds.people = data['people']
    for m in data['messages']:
        chat_path = ds.root / 'chat.md'
        lines = chat_path.read_text().splitlines() if chat_path.exists() else []
        line = next((i for i, text in enumerate(lines, 1) if re.match(rf'\|\s*{m["id"]}\s*\|', text)), None)
        source = link_message(chat_path, m['id'], line) if line else str(path.resolve())
        ds.messages.append(Message(m['id'], date_iso(m['at']), m['from'], m['text'], source))
    for key, values in data.items():
        match = re.fullmatch(r'rates_rub_per_([a-z]+)', key)
        if match:
            for date, rate in values.items():
                ds.rates[date, match[1].upper()] = decimal(rate)
                ds.rate_sources[date, match[1].upper()] = str(path.resolve())
    for r in data['receipts']:
        related = [m for m in ds.messages if m.id == r.get('source')]
        source = related[0].source if related else str(path.resolve())
        ds.receipts.append(Receipt(r['id'], r['date'], r['payer'], decimal(r['amount']), r['currency'], '', r['what'], r['for'], source, evidence=[m.source for m in related]))
    for r in data.get('refunds', []):
        related = [m for m in ds.messages if m.id == r.get('source')]
        source = related[0].source if related else str(path.resolve())
        ds.receipts.append(Receipt(r['id'], r['date'], r['to'], -decimal(r['amount']), r['currency'], '', r['what'], [], source, parent=r['receipt'], evidence=[m.source for m in related]))


def import_embedded(ds, path):
    # Deliberately ignore all result/check sections preceding raw source appendix.
    text = path.read_text()
    if '## Источники' not in text:
        raise ValueError('Документ не содержит отдельного раздела исходных источников')
    raw = text.split('## Источники', 1)[1]
    receipts = re.findall(r'<a (?:name|id)="(r\d+)"[^>]*></a>\*\*(R\d+)\*\* · (.*?)$', raw, re.M)
    year = next(iter(re.findall(r'\d\d\.\d\d\.(\d{4})', raw)), None)
    for id, at, author, body in re.findall(r'<a (?:name|id)="m\d+"[^>]*></a>\*\*(M\d+)\*\* · ([^·]+) · \*\*([^*]+)\*\*: (.*)$', raw, re.M):
        ds.messages.append(Message(id, date_iso(at, year), author, body, link_message(path, id)))
    ds.people = list(dict.fromkeys(m.author for m in ds.messages))
    for anchor, rid, rest in receipts:
        parts = [s.strip() for s in rest.split(' · ')]
        amount, currency = parse_amount(parts[3].replace('**', ''))[0]
        payer = re.search(r'оплатил\(а\) (\w+)', rest)[1]
        related = [m for m in ds.messages if rid in m.text]
        ds.receipts.append(Receipt(rid, date_iso(parts[0]), payer, amount, currency, parts[1], parts[2], infer_participants(ds, parts[2], related), str(path.resolve()) + '#' + anchor, evidence=[m.source for m in related]))
    ds.notes.append('Gist содержит готовый отчёт. Импорт использует только приложение «Источники»; таблицы ответов и проверки не участвуют в расчёте. Приложение чата неполное, полноту переписки подтвердить нельзя.')
    ds.issue('source_context_incomplete', 'В gist опубликован готовый отчёт с выборкой источников, а не полный исходный чат. Арифметика проверена по приложению источников; полнота поездки независимо не подтверждена.', [str(path.resolve()) + '#источники'])


def import_folder(ds):
    chat = ds.root / 'chat.md'
    if not chat.exists():
        raise ValueError('Не найден исходный чат')
    candidate_text = chat.read_text() + ''.join(p.read_text() for p in (ds.root / 'receipts').glob('*.md'))
    years = re.findall(r'\b(20\d\d)(?:-\d\d-\d\d|\b)', candidate_text)
    year = years[0] if years else None
    ds.messages = read_messages(chat, year)
    ds.people = participants_from_chat(chat.read_text(), [m.author for m in ds.messages])
    for path in sorted((ds.root / 'receipts').glob('*')):
        if not path.is_file() or path.suffix.lower() not in {'.md', '.png', '.jpg', '.jpeg'}:
            continue
        rid = path.stem
        related = [m for m in ds.messages if re.search(rf'\b{re.escape(rid)}\b', m.text)]
        if path.suffix.lower() != '.md':
            try:
                run = subprocess.run(['tesseract', str(path), 'stdout', '-l', 'eng'], capture_output=True, text=True, check=True)
                ds.ocr[rid] = run.stdout
                total = re.search(r'TOTAL\s+(\w{3})\s+([\d.,]+)', run.stdout)
                date = re.search(r'\d{4}-\d\d-\d\d', run.stdout)
                if not total or not date or not related:
                    raise ValueError('OCR не определил итог, дату или связанное сообщение')
                payer = related[0].author
                merchant = run.stdout.splitlines()[0].strip()
                ds.receipts.append(Receipt(rid, date[0], payer, decimal(total[2]), total[1], merchant, merchant, infer_participants(ds, merchant, related), str(path.resolve()), evidence=[m.source for m in related]))
            except (OSError, subprocess.CalledProcessError, ValueError) as error:
                ds.issue('unreadable_receipt', f'Не прочитан чек {rid}: {error}', [str(path.resolve())] + [m.source for m in related])
            continue
        text = path.read_text()
        meta = fields(text)
        refund = 'Исходный чек' in meta
        if 'Сумма' in meta:
            amount, currency = decimal(meta['Сумма']), meta['Валюта']
            if refund:
                amount = -abs(amount)
        else:
            amount, currency = parse_amount(meta['Итого'])[0]
        payer = meta.get('Плательщик', meta.get('Оплатил(а)', meta.get('Получатель')))
        participants = [p.strip() for p in meta['Участники'].split(',')] if 'Участники' in meta else infer_participants(ds, meta.get('Позиция', ''), related)
        weights = {n: decimal(v) for n, v in re.findall(rf'\|\s*([А-ЯЁ][а-яё]+)\s*\|\s*({AMOUNT})\s*\|', text)} or None
        ds.receipts.append(Receipt(rid, date_iso(meta['Дата'], year), payer, amount, currency, meta.get('Продавец', ''), meta.get('Позиция', text.split('\n\n')[1]), participants, str(path.resolve()), weights, meta.get('Исходный чек'), [m.source for m in related]))


def load(root):
    ds = Dataset(Path(root).resolve())
    if (ds.root / 'data.json').exists():
        import_json(ds, ds.root / 'data.json')
    elif (ds.root / 'chat.md').exists():
        import_folder(ds)
    else:
        files = list(ds.root.glob('*.md'))
        if len(files) != 1:
            raise ValueError('Неоднозначный формат входных данных')
        import_embedded(ds, files[0])
    if not ds.people or len(set(ds.people)) != len(ds.people):
        raise ValueError('Не определён уникальный список участников')
    read_rates(ds)
    finalize(ds)
    return ds


def message_mentions(ds, receipt):
    """Associate explicit IDs and conservative same-payer/date/amount echoes."""
    linked = []
    for message in ds.messages:
        if re.search(rf'\b{re.escape(receipt.id)}\b', message.text):
            linked.append(message)
        elif message.author == receipt.payer and message.date == receipt.date and not re.search(r'\bR\d+\b', message.text):
            if (abs(receipt.amount), receipt.currency) in parse_amount(message.text) and re.search(r'скинул|скинула|чек', message.text, re.I):
                linked.append(message)
    return linked


def scan_claims(ds):
    related_ids = {m.id for r in ds.receipts for m in message_mentions(ds, r)}
    confirmed_transfers = []
    for i, message in enumerate(ds.messages):
        text = message.text
        sources = [message.source]
        if re.search(r'это мо[её]|не делим|личн', text, re.I) and parse_amount(text):
            ds.issue('personal', 'Личная покупка исключена из общих расходов.', sources, state='excluded')
            continue
        if re.search(r'не помню сколько|чек (?:скину|пришлю)|скину вечером', text, re.I) and not parse_amount(text):
            later = [m for m in ds.messages[i + 1:] if m.author == message.author]
            withdrawn = next((m for m in later if 'забейте' in m.text.lower()), None)
            previous = next((issue for issue in reversed(ds.issues) if issue['kind'] == 'missing_amount' and issue['state'] == 'open' and any(m.author == message.author and m.source in issue['sources'] for m in ds.messages[:i])), None)
            if previous and re.search(r'скину вечером|чек.*вечером', text, re.I):
                previous['sources'].extend(sources)
            else:
                ds.issue('missing_amount', 'Расход заявлен без суммы/чека; не включён. ' + ('Заявитель позднее снял требование.' if withdrawn else 'Нужны сумма и источник оплаты.'), sources + ([withdrawn.source] if withdrawn else []), state='withdrawn' if withdrawn else 'open')
        if re.search(r'обеща\w* вернуть', text, re.I):
            amounts = parse_amount(text)
            later = [r for r in ds.receipts if r.amount < 0 and r.date >= message.date and (abs(r.amount), r.currency) in amounts]
            if later:
                ds.notes.append('Обещание возврата не проведено отдельно: найден последующий подтверждённый возврат.')
            else:
                ds.issue('unconfirmed_refund', 'Обещанный возврат не подтверждён получением; не включён.', sources)
        transfer_claim = re.search(r'\bуже (?:перев[её]л|вернул)\b|\b(?:переведу|верну)\b', text, re.I)
        transfer_claim = transfer_claim or (re.search(r'\bскину\b', text, re.I) and not re.search(r'чек|фото', text, re.I) and (re.search(r'тебе|вам', text, re.I) or any(name_in_phrase(n, text) for n in ds.people if n != message.author)))
        if transfer_claim:
            recipients = [n for n in ds.people if n != message.author and name_in_phrase(n, text.split(',')[0])]
            amounts = parse_amount(text)
            receiver = recipients[0] if len(recipients) == 1 else None
            confirmed = next((m for m in ds.messages[i + 1:] if m.author == receiver and re.search(r'получил|пришл[ио]|подтверждаю перевод', m.text, re.I) and len(amounts) == 1 and amounts[0] in parse_amount(m.text)), None)
            if receiver and len(amounts) == 1 and confirmed:
                amount, currency = amounts[0]
                confirmed_transfers.append(dict(sender=message.author, recipient=receiver, amount=str(amount), date=message.date, currency=currency, sources=sources + [confirmed.source]))
            else:
                ds.issue('unconfirmed_transfer', 'Перевод между участниками заявлен/обещан без достаточной суммы и подтверждения получателя; баланс не уменьшен.', sources)
        if message.id not in related_ids and re.search(r'заплатил|оплатил|списало|платил', text, re.I) and parse_amount(text) and not re.search(r'это мо[её]|не делим|личн|вернул', text, re.I):
            ds.issue('unmatched_expense', 'Дополнительная оплата не сопоставлена с чеком; не добавлена автоматически.', sources)
    # Compare linked claims to receipts. Ignore refund restatements ("теперь").
    for receipt in ds.receipts:
        if receipt.amount < 0:
            continue
        for message in message_mentions(ds, receipt):
            if re.search(r'вернул|возврат|теперь', message.text, re.I):
                continue
            amounts = [(a, c) for a, c in parse_amount(message.text) if c == receipt.currency]
            if amounts and abs(receipt.amount) not in [abs(a) for a, _ in amounts]:
                ds.issue('amount_mismatch', f'Заявленная оплата {receipt.id} не совпадает с итогом чека; в расчёте сохранена сумма чека, требуется сверка.', [receipt.source, message.source])
            if re.search(r'(?:платил|платила|оплатил|оплатила|списало).*?я|(?:оплатил|оплатила|заплатил|заплатила)', message.text, re.I) and message.author != receipt.payer:
                ds.issue('payer_mismatch', f'Плательщик в сообщении и чеке {receipt.id} различается.', [receipt.source, message.source])
    return confirmed_transfers


def finalize(ds):
    """Resolve evidence into a stable contract, retaining every uncertainty."""
    canonical = {}
    conflicts = set()
    for receipt in ds.receipts:
        if receipt.id not in canonical:
            canonical[receipt.id] = receipt
            continue
        old = canonical[receipt.id]
        identity = lambda r: (r.date, r.payer, r.amount, r.currency, r.merchant, r.parent, r.participants, r.weights)
        if identity(old) != identity(receipt):
            conflicts.add(receipt.id)
            ds.issue('duplicate_conflict', f'ID {receipt.id} описывает разные операции; обе версии исключены до сверки.', [old.source, receipt.source])
        else:
            old.evidence.extend([receipt.source] + receipt.evidence)
            ds.notes.append(f'Повторная запись операции {receipt.id} объединена по ID и совпадающим полям.')
    ds.receipts = [r for id, r in canonical.items() if id not in conflicts]
    for receipt in ds.receipts:
        mentions = message_mentions(ds, receipt)
        receipt.evidence = list(dict.fromkeys(receipt.evidence + [m.source for m in mentions]))
        if receipt.amount < 0 and not receipt.parent:
            candidates = [r for r in ds.receipts if r.amount > 0 and r.payer == receipt.payer and r.merchant == receipt.merchant and r.currency == receipt.currency and r.date <= receipt.date and r.amount >= abs(receipt.amount)]
            if len(candidates) == 1:
                receipt.parent = candidates[0].id
        payment_mentions = [m for m in mentions if not re.search(r'вернул|возврат|теперь', m.text, re.I)]
        if receipt.amount > 0 and len(payment_mentions) > 1:
            ds.duplicates.append(dict(receipt=receipt.id, messages=[m.id for m in payment_mentions], sources=[receipt.source] + [m.source for m in payment_mentions]))
    ds.confirmed_transfers = scan_claims(ds)
    for message in ds.messages:
        if not re.search(r'удержал\w* за (?:разбит|ущерб|поврежд)', message.text, re.I):
            continue
        match = re.search(rf'({AMOUNT})\s*({CURRENCY})\s+удержал', message.text, re.I)
        if not match:
            continue
        currency = SYMBOLS.get(match[2].lower(), match[2].upper())
        penalty = cents(decimal(match[1]) * ds.rate(message.date, currency))
        candidates = [r for r in ds.receipts if r.amount > 0 and r.payer == message.author and ('депозит' in r.description.lower() or any('депозит' in m.text.lower() for m in message_mentions(ds, r)))]
        if len(candidates) != 1:
            ds.issue('deposit_unlinked', 'Удержание из депозита не сопоставлено однозначно с расходом.', [message.source], penalty)
            continue
        receipt = candidates[0]
        questions = [m.source for m in ds.messages if m.date >= message.date and re.search(r'кто разбил|не я', m.text, re.I)]
        claim_sources = [message.source, receipt.source] + questions
        ds.pending_allocations.append(dict(receipt=receipt.id, amount=str(decimal(match[1])), date=message.date, currency=currency, sources=claim_sources))
        ds.issue('disputed_allocation', 'Удержание за ущерб подтверждено, но виновник и согласованное распределение не установлены. Основной расчёт оставляет сумму нераспределённой; equal-provisional — отдельный условный сценарий поровну.', claim_sources, penalty)
    # Unknown financially relevant messages are evidence gaps, not inferred debts.
    covered = {m.id for r in ds.receipts for m in message_mentions(ds, r)}
    for message in ds.messages:
        if message.id in covered or any(message.source in issue['sources'] for issue in ds.issues):
            continue
        if re.search(r'оплатил|заплатил|вернул|перев[её]л', message.text, re.I) and not re.search(r'не (?:оплатил|платил)|билет.*личн', message.text, re.I):
            ds.issue('unclassified_claim', 'Финансовая реплика не сопоставлена с операцией; требуется просмотр, автоматического долга нет.', [message.source])


def snapshot(ds):
    return dict(schema_version=1, root=str(ds.root), people=ds.people, messages=[asdict(m) for m in ds.messages], receipts=[asdict(r) for r in ds.receipts], rates=[dict(date=date, currency=currency, rub_per_unit=str(rate), source=ds.rate_sources.get((date, currency))) for (date, currency), rate in sorted(ds.rates.items())], issues=ds.issues, notes=ds.notes, ocr=ds.ocr, confirmed_transfers=ds.confirmed_transfers, pending_allocations=ds.pending_allocations, duplicates=ds.duplicates)


def load_snapshot(path):
    data = json.loads(Path(path).read_text())
    if data.get('schema_version') != 1:
        raise ValueError('Расчётный слой принимает только normalized JSON schema_version=1')
    ds = Dataset(Path(data['root']))
    ds.people = data['people']
    if not ds.people or len(ds.people) != len(set(ds.people)):
        raise ValueError('Некорректный список участников')
    ds.messages = [Message(**m) for m in data['messages']]
    for item in data['receipts']:
        item['amount'] = decimal(item['amount'])
        if item.get('weights'):
            item['weights'] = {n: decimal(v) for n, v in item['weights'].items()}
        ds.receipts.append(Receipt(**item))
    for rate in data['rates']:
        if decimal(rate['rub_per_unit']) <= 0:
            raise ValueError('Валютный курс должен быть положительным')
        key = rate['date'], rate['currency']
        ds.rates[key] = decimal(rate['rub_per_unit'])
        ds.rate_sources[key] = rate['source']
    for key in ['issues', 'notes', 'ocr', 'confirmed_transfers', 'pending_allocations', 'duplicates']:
        setattr(ds, key, data[key])
    return ds


def main():
    parser = argparse.ArgumentParser(description='Normalize source formats without changing source files.')
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    ds = load(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot(ds), ensure_ascii=False, indent=2, default=str) + '\n')
    for id, text in ds.ocr.items():
        args.output.with_name(f'{id}-ocr.txt').write_text(text)
    print(json.dumps(dict(input=str(ds.root), normalized=str(args.output), people=len(ds.people), messages=len(ds.messages), receipts=len(ds.receipts), issues=len(ds.issues), schema_version=1), ensure_ascii=False))


if __name__ == '__main__':
    main()
