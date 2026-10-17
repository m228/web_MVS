"""Экранирование имени сетевого адаптера для PowerShell (анти-инъекция).

Доказывает фикс: типографские кавычки U+2018…U+201B PowerShell трактует как
разделители одинарной строки, поэтому их НЕЛЬЗЯ пропускать в '...'. До фикса
имя "a’; Write-Output INJECTED; ’b" вырывалось из строки и выполняло команду.
"""
import net_tools


def test_normal_names_pass_through():
    assert net_tools._ps_adapter("Ethernet") == "Ethernet"
    assert net_tools._ps_adapter("Подключение по локальной сети") == "Подключение по локальной сети"
    # пробелы, скобки, звёздочка, # — валидные символы имён адаптеров Windows
    assert net_tools._ps_adapter("vEthernet (WSL) #2 *") == "vEthernet (WSL) #2 *"


def test_ascii_apostrophe_is_doubled():
    # ASCII-апостроф остаётся внутри '...' через удвоение — это безопасно
    assert net_tools._ps_adapter("O'Brien NIC") == "O''Brien NIC"


def test_empty_and_whitespace_rejected():
    assert net_tools._ps_adapter("") is None
    assert net_tools._ps_adapter("   ") is None
    assert net_tools._ps_adapter(None) is None


def test_control_chars_rejected():
    for bad in ("a\nb", "a\rb", "a\x00b"):
        assert net_tools._ps_adapter(bad) is None


def test_smart_quotes_rejected():
    # ключевой случай: без этого была инъекция PowerShell
    for q in ("‘", "’", "‚", "‛"):
        name = "a%s; Write-Output INJECTED; %sb" % (q, q)
        assert net_tools._ps_adapter(name) is None, "smart-quote %r должен отвергаться" % q
