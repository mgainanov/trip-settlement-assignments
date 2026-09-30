"""Format-independent accounting engine. Input is normalized schema v1 JSON only."""
from __future__ import annotations
import argparse
import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from normalize import Dataset, Receipt, cents, decimal, rub, load_snapshot
D = Decimal

def allocate(total, weights, order):
    """Largest-remainder allocation preserving exact cents; ties follow roster."""
    if not weights or sum(weights.values()) <= 0 or any(v < 0 for v in weights.values()):
        raise ValueError('Неверные веса распределения')
    sign = -1 if total < 0 else 1
    values = {n: D(abs(total)) * w / sum(weights.values()) for n, w in weights.items()}
    floor = {n: int(v) for n, v in values.items()}
    ranked = sorted(weights, key=lambda n: (-(values[n] - floor[n]), order.index(n)))
    for n in ranked[:abs(total) - sum(floor.values())]:
        floor[n] += 1
    return {n: sign * value for n, value in floor.items()}


def plan_transfers(balances):
    names = [n for n, amount in balances.items() if amount]
    if len(names) > 12:
        raise ValueError('Точный перебор минимума ограничен 12 ненулевыми балансами')
    zero = [mask for mask in range(1, 1 << len(names)) if sum(balances[names[i]] for i in range(len(names)) if mask >> i & 1) == 0]
    memo = {0: []}
    def best(mask):
        if mask in memo:
            return memo[mask]
        first = mask & -mask
        candidates = [group for group in zero if group & first and group & mask == group]
        choices = [[group] + best(mask ^ group) for group in candidates]
        memo[mask] = max(choices, key=len)
        return memo[mask]
    groups = best((1 << len(names)) - 1)
    transfers = []
    for mask in groups:
        debtors = [[n, -balances[n]] for i, n in enumerate(names) if mask >> i & 1 and balances[n] < 0]
        creditors = [[n, balances[n]] for i, n in enumerate(names) if mask >> i & 1 and balances[n] > 0]
        debtors.sort(key=lambda pair: -pair[1])
        creditors.sort(key=lambda pair: -pair[1])
        for sender in debtors:
            for recipient in creditors:
                amount = min(sender[1], recipient[1])
                if amount:
                    transfers.append(dict(sender=sender[0], recipient=recipient[0], cents=amount))
                    sender[1] -= amount
                    recipient[1] -= amount
    minimum = len(names) - len(groups)
    assert len(transfers) == minimum
    return transfers, minimum


def solve(ds, scenario='confirmed'):
    rows = []
    purchases = {}
    for receipt in ds.receipts:
        if receipt.amount <= 0:
            continue
        if receipt.payer not in ds.people or not receipt.participants or not set(receipt.participants) <= set(ds.people):
            ds.issue('invalid_participants', f'Не определены плательщик/участники {receipt.id}.', [receipt.source])
            continue
        if receipt.id in purchases:
            old = purchases[receipt.id]['receipt']
            identity = lambda r: (r.date, r.payer, r.amount, r.currency, r.merchant)
            if identity(old) != identity(receipt):
                ds.issue('duplicate_conflict', f'Разные операции используют один ID {receipt.id}; второе представление не проведено.', [old.source, receipt.source])
            else:
                purchases[receipt.id]['sources'].append(receipt.source)
            continue
        try:
            rate = ds.rate(receipt.date, receipt.currency)
        except ValueError as error:
            ds.issue('missing_rate', str(error), [receipt.source])
            continue
        weights = receipt.weights or {n: D(1) for n in receipt.participants}
        if receipt.weights and (set(weights) != set(receipt.participants) or sum(weights.values()) != receipt.amount):
            ds.issue('item_total_mismatch', f'Индивидуальные позиции {receipt.id} не сходятся с итогом; расход не распределён.', [receipt.source])
            continue
        value = cents(receipt.amount * rate)
        sources = list(dict.fromkeys([receipt.source] + receipt.evidence))
        if receipt.currency != 'RUB':
            sources.append(ds.rate_sources[receipt.date, receipt.currency])
        row = dict(id=receipt.id, receipt=receipt, payer=receipt.payer, date=receipt.date, currency=receipt.currency, amount=str(receipt.amount), rate=str(rate), cash_cents=value, eligible_cents=value, shares=allocate(value, weights, ds.people), sources=sources, status='included')
        purchases[receipt.id] = row
        rows.append(row)
    native_refunded = defaultdict(Decimal)
    for receipt in ds.receipts:
        if receipt.amount >= 0:
            continue
        parent = purchases.get(receipt.parent)
        if not parent:
            candidates = [row for row in purchases.values() if row['receipt'].payer == receipt.payer and row['receipt'].merchant == receipt.merchant and row['receipt'].currency == receipt.currency and row['receipt'].date <= receipt.date and row['receipt'].amount >= abs(receipt.amount)]
            if len(candidates) == 1:
                parent = candidates[0]
        if not parent:
            ds.issue('unlinked_refund', f'Возврат {receipt.id} не имеет однозначного исходного расхода; не проведён.', [receipt.source])
            continue
        original = parent['receipt']
        if original.payer != receipt.payer or original.currency != receipt.currency or receipt.date < original.date:
            ds.issue('refund_mismatch', f'Получатель, валюта или дата возврата {receipt.id} не соответствуют исходному расходу.', [receipt.source, original.source])
            continue
        native_refunded[original.id] += abs(receipt.amount)
        if native_refunded[original.id] > original.amount:
            ds.issue('excess_refund', f'Возвраты {original.id} превышают исходную сумму в валюте.', [receipt.source, original.source])
            continue
        try:
            rate = ds.rate(receipt.date, receipt.currency)
        except ValueError as error:
            ds.issue('missing_rate', str(error), [receipt.source])
            continue
        value = cents(receipt.amount * rate)
        weights = original.weights or {n: D(1) for n in original.participants}
        sources = [receipt.source, original.source] + receipt.evidence
        if receipt.currency != 'RUB':
            sources.append(ds.rate_sources[receipt.date, receipt.currency])
        rows.append(dict(id=receipt.id, payer=receipt.payer, date=receipt.date, currency=receipt.currency, amount=str(receipt.amount), rate=str(rate), cash_cents=value, eligible_cents=value, shares=allocate(value, weights, ds.people), sources=list(dict.fromkeys(sources)), parent=original.id, status='refund'))
    # Normalizer supplies unresolved allocation claims; no raw text in the engine.
    pending = 0
    for claim in ds.pending_allocations:
        row = purchases.get(claim['receipt'])
        if not row:
            continue
        penalty = cents(decimal(claim['amount']) * ds.rate(claim['date'], claim['currency']))
        if scenario == 'confirmed':
            pending += penalty
            row['eligible_cents'] -= penalty
            penalty_shares = allocate(penalty, row['receipt'].weights or {n: D(1) for n in row['receipt'].participants}, ds.people)
            for name, value in penalty_shares.items():
                row['shares'][name] -= value
            row['status'] = 'partial_allocation_pending'
        else:
            row['status'] = 'provisional_equal_allocation'
    known_transfers = [dict(t, cents=cents(decimal(t['amount']) * ds.rate(t['date'], t['currency']))) for t in ds.confirmed_transfers]
    paid, shares = defaultdict(int), defaultdict(int)
    for row in rows:
        paid[row['payer']] += row['eligible_cents']
        for name, value in row['shares'].items():
            shares[name] += value
        assert sum(row['shares'].values()) == row['eligible_cents']
    balances = {name: paid[name] - shares[name] for name in ds.people}
    for transfer in known_transfers:
        balances[transfer['sender']] += transfer['cents']
        balances[transfer['recipient']] -= transfer['cents']
    assert sum(balances.values()) == 0
    plan, minimum = plan_transfers(balances)
    remaining = balances.copy()
    for transfer in plan:
        remaining[transfer['sender']] += transfer['cents']
        remaining[transfer['recipient']] -= transfer['cents']
    assert not any(remaining.values()) and len(plan) <= max(0, len(ds.people) - 1)
    cash_total = sum(row['cash_cents'] for row in rows)
    allocated_total = sum(row['eligible_cents'] for row in rows)
    assert cash_total - allocated_total == pending
    duplicates = ds.duplicates
    blocking = [i for i in ds.issues if i['state'] == 'open' and i['kind'] != 'personal']
    for row in rows:
        row.pop('receipt', None)
    return dict(people=ds.people, scenario=scenario, cash_total_cents=cash_total, allocated_total_cents=allocated_total, pending_allocation_cents=pending, gross_cents=sum(r['cash_cents'] for r in rows if r['cash_cents'] > 0), refunds_cents=-sum(r['cash_cents'] for r in rows if r['cash_cents'] < 0), paid_cents=dict(paid), shares_cents=dict(shares), balances_cents=balances, plan=plan, minimum_transfers=minimum, known_transfers=known_transfers, final_settlement_ready=not blocking, disputes=ds.issues, duplicates=duplicates, notes=list(dict.fromkeys(ds.notes)), rows=rows, checks=dict(balances_zero_sum=True, exact_row_allocation=True, paid_equals_shares=True, plan_closes_balances=True, minimum_proven=True, n_minus_one=True), source_data_edits=0)


def source_link(source, output):
    import os
    file, _, fragment = source.partition('#')
    target = file if source.startswith(('https://', 'http://')) else os.path.relpath(file, output)
    return '[' + Path(file).name + (('#' + fragment) if fragment else '') + '](' + target.replace(' ', '%20') + (('#' + fragment) if fragment else '') + ')'


def render(result, output):
    def links(sources):
        return ', '.join(source_link(s, output) for s in dict.fromkeys(sources))
    all_sources = [source for row in result['rows'] for source in row['sources']]
    lines = ['# Расчёт поездки', '', f"Сценарий: `{result['scenario']}`. Исходники не исправлялись: 0 ручных правок данных.", '', f"Подтверждённые чистые оплаты: **{rub(result['cash_total_cents'])} ₽**. Распределено: **{rub(result['allocated_total_cents'])} ₽**. Ожидает согласования распределения: **{rub(result['pending_allocation_cents'])} ₽**.", '', 'Расчёт окончательный.' if result['final_settlement_ready'] else '**Есть спорные записи. Балансы и план относятся только к указанному сценарию и не являются окончательным закрытием всех претензий.**', '', '| ID | Дата | Кто оплатил / получил возврат | Сумма | Курс | Чистая оплата ₽ | В расчёте ₽ | Статус | Источники |', '|---|---|---|---|---|---|---|---|---|']
    for row in result['rows']:
        lines.append(f"| {row['id']} | {row['date']} | {row['payer']} | {row['amount']} {row['currency']} | {row['rate']} | {rub(row['cash_cents'])} | {rub(row['eligible_cents'])} | {row['status']} | {links(row['sources'])} |")
    lines += ['', '## Доли по операциям', '', '| ID | ' + ' | '.join(result['people']) + ' | Источники |', '|---|' + '|'.join('---' for _ in result['people']) + '|---|']
    for row in result['rows']:
        lines.append('| ' + row['id'] + ' | ' + ' | '.join(rub(row['shares'].get(n, 0)) for n in result['people']) + ' | ' + links(row['sources']) + ' |')
    lines += ['', '## Балансы', '', '| Участник | Оплаты, допущенные к распределению ₽ | Доля ₽ | Баланс ₽ | Источники |', '|---|---|---|---|---|']
    for name in result['people']:
        sources = [s for row in result['rows'] if name == row['payer'] or name in row['shares'] for s in row['sources']]
        sources += [s for t in result['known_transfers'] if name in {t['sender'], t['recipient']} for s in t['sources']]
        lines.append(f"| {name} | {rub(result['paid_cents'].get(name, 0))} | {rub(result['shares_cents'].get(name, 0))} | {rub(result['balances_cents'][name])} | {links(sources)} |")
    lines += ['', '## Переводы', '', f"Минимум {result['minimum_transfers']}: найдено максимальное число групп с нулевой суммой, внутри каждой группа закрывается за размер минус один перевод. Граница n−1 проверена.", '', '| Кто | Кому | ₽ | Основание |', '|---|---|---|---|']
    for t in result['plan']:
        lines.append(f"| {t['sender']} | {t['recipient']} | {rub(t['cents'])} | [Балансы](#балансы); {links(all_sources)} |")
    lines += ['', '## Спорное', '', '| Вид | Что требует проверки | Сумма ₽ | Состояние | Источники |', '|---|---|---|---|---|']
    for issue in result['disputes']:
        lines.append(f"| {issue['kind']} | {issue['text']} | {rub(issue['amount_rub']) if issue['amount_rub'] is not None else 'не установлена'} | {issue['state']} | {links(issue['sources'])} |")
    if not result['disputes']:
        lines.append('| — | Спорных записей не обнаружено | — | — | — |')
    lines += ['', '## Повторные упоминания чеков', '']
    for duplicate in result['duplicates']:
        lines.append(f"- {duplicate['receipt']}: сообщения {', '.join(duplicate['messages'])}; одна операция, повторные сообщения не добавляют расходы. {links(duplicate['sources'])}")
    lines += ['', '## Проверки и допущения', '']
    lines += [f'- {key}: {value}.' for key, value in result['checks'].items()]
    lines += ['- ' + note for note in result['notes']]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--scenario', choices=['confirmed', 'equal-provisional'], default='confirmed')
    args = parser.parse_args()
    ds = load_snapshot(args.input)
    result = solve(ds, args.scenario)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    (args.output / 'report.md').write_text(render(result, args.output))
    (args.output / 'normalized.sha256').write_text(hashlib.sha256(args.input.read_bytes()).hexdigest() + '\n')
    print(json.dumps(dict(input=str(ds.root), output=str(args.output.resolve()), cash_total_rub=rub(result['cash_total_cents']), allocated_rub=rub(result['allocated_total_cents']), disputed=len(result['disputes']), final=result['final_settlement_ready'], checks=result['checks']), ensure_ascii=False))


if __name__ == '__main__':
    main()
