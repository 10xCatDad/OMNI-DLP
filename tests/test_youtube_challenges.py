"""Independently authored signature programs, not downloaded player fixtures."""
from dataclasses import FrozenInstanceError
import hashlib

import pytest

import omni_dlp.youtube_challenges as challenges
from omni_dlp.youtube_challenges import (
    SignatureOperation as Op,
    SignaturePlan,
    UnsupportedChallenge,
    apply_signature,
    compile_signature,
)


HELPERS = '''var toolkit={
    back:function(items){items.reverse()},
    cut:function(items,count){items.splice(0,count)},
    exchange:function(items,count){var saved=items[0];
        items[0]=items[count%items.length];items[count%items.length]=saved}
};'''


def program(body: str, *, helpers=HELPERS) -> str:
    return helpers + 'decode=function(value){value=value.split("");' + body + ';return value.join("")};'


def dispatcher(*, logical=False) -> str:
    # Small invented alphabet/key and unrelated names exercise the obfuscation
    # grammar observed in public players without copying a player implementation.
    declarations = '''
      var lexicon="split|join|reverse|splice|length||flip|trim|permute".split("|");
      var tools={
        flip:function(row){row[lexicon[2]]()},
        trim:function(row,n){row[lexicon[3]](0,n)},
        permute:function(row,n){var saved=row[0];
            row[0]=row[n%row[lexicon[4]]];row[n%row[lexicon[4]]]=saved}
      };
    '''
    # 5 ^ 30 == 27; ^ constants resolve table slots and operation arguments.
    body = '''row=text[lexicon[key^27]](lexicon[key^30]),
        tools[lexicon[key^29]](row,key^91),
        tools[lexicon[key^19]](row,key^30),
        tools[lexicon[key^28]](row,2),
        answer=row[lexicon[key^26]](lexicon[key^30])'''
    if logical:
        branch = '(mode&3)==1&&(' + body + ');'
    else:
        branch = 'if(mode===5){' + body.replace(',\n', ';\n') + ';}'
    return declarations + '''
      route=function(mode,seed,text){var key=seed^mode;
        if(mode===0){var row=outside();var answer=unrecognized([1,2,3])}
    ''' + branch + '''
        (mode>>1&3)==0&&unrecognized({body:/[{}]/});
        if(mode===9)for(var k=0;k<10;k++)outside(k);
        return answer};
      result=route(5,30,packet.s);
    '''


def test_helpers_produce_plan_and_source_digest():
    source = program('toolkit.back(value);toolkit.exchange(value,3);toolkit.cut(value,1)')
    plan = compile_signature(source)
    assert plan.operations == (Op('reverse'), Op('swap', 3), Op('drop', 1))
    assert apply_signature(plan, 'abcdef') == 'edfba'
    assert plan.source_sha256 == hashlib.sha256(source.encode()).hexdigest()


@pytest.mark.parametrize('logical', [False, True])
def test_table_xor_dispatcher_with_unparsed_inactive_branches(logical):
    plan = compile_signature(dispatcher(logical=logical))
    assert plan.operations == (Op('reverse'), Op('swap', 5), Op('drop', 2))
    assert apply_signature(plan, 'abcdefgh') == 'fedhba'


@pytest.mark.parametrize('old,new', [
    ('lexicon', '$alphabet'), ('tools', '_mutators'), ('route', 'processSignature'),
    ('row', '$x'), ('key', 'mask'), ('flip', 'a'), ('trim', 'b'), ('permute', 'c'),
])
def test_discovery_does_not_depend_on_player_identifier_names(old, new):
    plan = compile_signature(dispatcher().replace(old, new))
    assert plan.operations == (Op('reverse'), Op('swap', 5), Op('drop', 2))


def test_multiple_equivalent_dispatch_call_sites_are_allowed():
    source = dispatcher(logical=True) + 'other=route(13,22,packet.other);'
    assert compile_signature(source).operations == (Op('reverse'), Op('swap', 5), Op('drop', 2))


def test_conflicting_dispatch_call_sites_are_ambiguous():
    source = dispatcher(logical=True).replace('tools[lexicon[key^19]](row,key^30)',
                                              'tools[lexicon[key^19]](row,mode)')
    with pytest.raises(UnsupportedChallenge, match='unique'):
        compile_signature(source + 'other=route(13,22,packet.other);')


def test_inactive_signature_call_does_not_supply_unverified_key():
    with pytest.raises(UnsupportedChallenge):
        compile_signature(dispatcher().replace('route(5,30,packet.s)', 'route(0,27,packet.s)'))


def test_missing_constant_call_sites_rejected():
    source = dispatcher().replace('route(5,30,packet.s)', 'route(unknown,unknown2,packet.s)')
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


@pytest.mark.parametrize('body,expected', [
    ('value.reverse()', 'fedcba'),
    ('value.splice(0,2);value.reverse()', 'fedc'),
    ('value=value.slice(2);value.reverse()', 'fedc'),
])
def test_direct_operations(body, expected):
    assert apply_signature(compile_signature(program(body, helpers='')), 'abcdef') == expected


def test_returning_slice_helper():
    source = program('value=toolkit.cut(value,2);value.reverse()',
                     helpers='var toolkit={cut:function(a,n){return a.slice(n)}};')
    assert apply_signature(compile_signature(source), 'abcdef') == 'fedc'


def test_returning_reverse_helper_preserves_truthy_array_result():
    source = program('var flag=toolkit.back(value);if(flag){value.splice(0,1)}',
                     helpers='var toolkit={back:function(a){return a.reverse()}};')
    assert apply_signature(compile_signature(source), 'abcdef') == 'edcba'


@pytest.mark.parametrize('body', [
    'var f=value.reverse;f()',
    'var f=value.join;value.reverse();return f("")',
])
def test_unbound_array_methods_rejected(body):
    with pytest.raises(UnsupportedChallenge):
        compile_signature(program(body, helpers=''))


def test_unbound_string_method_rejected():
    source = 'decode=function(a){var f=a.split;a=f("");a.reverse();return a.join("")};'
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


@pytest.mark.parametrize('helpers', [
    'if(false){' + HELPERS + '}',
    'if(false)' + HELPERS,
    'false&&(' + HELPERS.replace('var ', '').rstrip(';') + ');',
    'function make(){' + HELPERS + '}',
])
def test_conditional_or_unrelated_scope_helper_declarations_rejected(helpers):
    with pytest.raises(UnsupportedChallenge):
        compile_signature(program('toolkit.back(value)', helpers=helpers))


@pytest.mark.parametrize('body', [
    '{let value=1};if(value){value.reverse()}',
    'const x=0;x=1;if(x){value.reverse()}',
])
def test_block_scoping_and_const_mutation_are_not_assumed(body):
    with pytest.raises(UnsupportedChallenge):
        compile_signature(program(body))


@pytest.mark.parametrize('body', [
    'value.slice(2);value.reverse()',
    '(value.slice(2),value.reverse())',
    'var discarded=value.slice(2);value.reverse()',
    'toolkit.cut(value,2);value.reverse()',
])
def test_slice_result_must_replace_its_array(body):
    source = program(body, helpers='var toolkit={cut:function(a,n){return a.slice(n)}};')
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


def test_slice_alias_cannot_be_mistaken_for_mutated_original():
    source = '''decode=function(value){value=value.split("");var alias=value;
        value=value.slice(2);return alias.join("")};'''
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


@pytest.mark.parametrize('replacement', [
    'items.reverse();send(items)',
    'send(items);items.reverse()',
    'items.reverse();return other',
    'items.reverse();items[0]="x"',
    'items.sort()',
    'items.reverse();while(true){}',
    'return items.reverse(),send(items)',
])
def test_extra_or_unknown_helper_statements_rejected(replacement):
    source = program('toolkit.back(value)', helpers=HELPERS.replace('items.reverse()', replacement))
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


@pytest.mark.parametrize('extra', [
    'toolkit.back=function(a){send(a)};',
    'toolkit["back"]=other;',
    'delete toolkit.back;',
    'toolkit=other;',
    'mutate(toolkit);',
    'function shadow(toolkit){return toolkit}',
])
def test_helper_rebinding_mutation_or_escape_rejected(extra):
    with pytest.raises(UnsupportedChallenge):
        compile_signature(program('toolkit.back(value)') + extra)


@pytest.mark.parametrize('extra', [
    'lexicon[0]="wrong";', 'lexicon=other;', 'mutate(lexicon);',
    'function shadow(lexicon){return lexicon}',
    'lexicon[0]^=3;', 'lexicon[0]%=2;', 'lexicon[0]<<=1;',
])
def test_table_rebinding_mutation_or_escape_rejected(extra):
    with pytest.raises(UnsupportedChallenge):
        compile_signature(dispatcher() + extra)


@pytest.mark.parametrize('extra', [
    'decode=other;',
    'decode=function(a){send(a)};',
    'function shadow(decode){return decode}',
])
def test_candidate_rebinding_or_shadowing_rejected(extra):
    with pytest.raises(UnsupportedChallenge):
        compile_signature(program('toolkit.back(value)') + extra)


def test_duplicate_helper_keys_rejected():
    source = program('toolkit.back(value)', helpers=HELPERS.replace('cut:function', 'back:function'))
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


def test_two_signature_functions_are_ambiguous_even_with_same_plan():
    source = program('toolkit.back(value)')
    source += program('toolkit.back(value)', helpers='').replace('decode=', 'another=')
    with pytest.raises(UnsupportedChallenge, match='unique'):
        compile_signature(source)


@pytest.mark.parametrize('body', [
    'value.reverse();send(value)',
    'value.reverse();unrelated=value',
    'toolkit.back("unrelated")',
    'value=value.split("");value.reverse()',
    'var stolen=value.join("");value.reverse()',
    'value.reverse();value="replacement"',
    'if(value.length){value.reverse()}',
    'while(true){value.reverse()}',
    'value.reverse();return "constant"',
    'value.reverse();return value.join("");send(value)',
])
def test_invalid_dataflow_or_extra_active_statements_rejected(body):
    with pytest.raises(UnsupportedChallenge):
        compile_signature(program(body))


@pytest.mark.parametrize('declaration', [
    'if(false){let leaked}', 'if(false){function nested(){var leaked}}',
])
def test_other_scope_declarations_do_not_authorize_global_writes(declaration):
    source = program(declaration + ';leaked=value;value.reverse()')
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


@pytest.mark.parametrize('separator', ['\n', '/*\n*/', '\r\n', '\u2028'])
def test_return_automatic_semicolon_insertion(separator):
    source = program('toolkit.back(value)').replace('return value', 'return' + separator + 'value')
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


def test_helper_return_automatic_semicolon_insertion():
    source = program('value=toolkit.cut(value,2);value.reverse()',
                     helpers='var toolkit={cut:function(a,n){return\n a.slice(n)}};')
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


def test_comments_strings_regex_literals_cannot_create_candidates():
    noise = '''var text="fake=function(a){a=a.split('');a.reverse();return a.join('')}";
    var regex=/[{};]fake=function\\(a\\)/;
    /* other=function(a){a=a.split("");a.reverse();return a.join("")} */
    // final=function(a){a=a.split("");a.reverse();return a.join("")}
    '''
    assert compile_signature(noise + program('toolkit.back(value)')).operations == (Op('reverse'),)


def test_escaped_property_names_and_scientific_numeric_literals():
    source = program('toolkit.exchange(value,3E1);toolkit.cut(value,0x2)')
    source = source.replace('items.reverse()', 'items["\\x72everse"]()')
    source = source.replace('items.length', 'items["l\\u0065ngth"]')
    plan = compile_signature(source)
    assert plan.operations == (Op('swap', 30), Op('drop', 2))
    assert apply_signature(plan, 'abcdefg') == 'adefg'


@pytest.mark.parametrize('source', [
    'var a="unterminated', '/* unterminated', 'var a=/unterminated',
    'function x(a){', 'function x(a){]}', 'var x=`${untrusted()}`;',
    'var x="\\uZZZZ";', 'var x="\\777";',
])
def test_malformed_or_unsupported_lexical_input_rejected(source):
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


def test_token_budget(monkeypatch):
    monkeypatch.setattr(challenges, 'MAX_TOKENS', 12)
    with pytest.raises(UnsupportedChallenge, match='token limit'):
        compile_signature(program('toolkit.back(value)'))


def test_source_budget(monkeypatch):
    monkeypatch.setattr(challenges, 'MAX_SOURCE', 20)
    with pytest.raises(UnsupportedChallenge, match='source size'):
        compile_signature(program('toolkit.back(value)'))


def test_nesting_budget():
    with pytest.raises(UnsupportedChallenge, match='nesting limit'):
        compile_signature('(' * 129 + '1' + ')' * 129)


def test_operation_budget():
    with pytest.raises(UnsupportedChallenge):
        compile_signature(program('value.reverse();' * 129))


@pytest.mark.parametrize('argument', ['-1', '1e1000', '2.5', '0/0', '2147483648', 'unknown'])
def test_unsafe_or_unknown_operation_argument_rejected(argument):
    with pytest.raises(UnsupportedChallenge):
        compile_signature(program(f'toolkit.exchange(value,{argument})'))


def test_plan_and_operations_are_immutable():
    plan = SignaturePlan((Op('swap', 2),))
    with pytest.raises(FrozenInstanceError):
        plan.operations = ()
    with pytest.raises(FrozenInstanceError):
        plan.operations[0].argument = 3
    with pytest.raises(UnsupportedChallenge):
        SignaturePlan([Op('reverse')])


@pytest.mark.parametrize('value', ['a', 'ab', 'abc', 'abcd', 'abcde', 'abcdefghijklmnop'])
def test_swap_uses_current_input_length(value):
    plan = SignaturePlan((Op('reverse'), Op('swap', 7), Op('drop', 1)))
    expected = list(reversed(value))
    position = 7 % len(expected)
    expected[0], expected[position] = expected[position], expected[0]
    assert apply_signature(plan, value) == ''.join(expected[1:])


def test_prefix_removal_changes_swap_modulus():
    plan = SignaturePlan((Op('drop', 2), Op('swap', 7)))
    assert apply_signature(plan, 'abcdef') == 'fdec'


def test_empty_array_swap_is_explicitly_unsupported():
    with pytest.raises(UnsupportedChallenge, match='empty'):
        apply_signature(SignaturePlan((Op('drop', 100), Op('swap', 1))), 'small')
    assert apply_signature(SignaturePlan((Op('reverse'),)), '') == ''


def test_utf16_semantics_include_surrogate_pairs_and_lone_surrogates():
    plan = SignaturePlan((Op('reverse'),))
    assert apply_signature(plan, 'A\U0001f600B') == 'B\ude00\ud83dA'
    assert apply_signature(plan, '\ud800x') == 'x\ud800'
    assert apply_signature(SignaturePlan((Op('drop', 1),)), '\U0001f600x') == '\ude00x'


def test_input_budget_counts_utf16_units():
    with pytest.raises(UnsupportedChallenge, match='input limit'):
        apply_signature(SignaturePlan((Op('reverse'),)), '\U0001f600' * 8193)


def test_splice_returns_truthy_removed_array():
    source = program('var flag=value.splice(0,1);if(flag){value.reverse()}', helpers='')
    assert apply_signature(compile_signature(source), 'abcdef') == 'fedcb'


def test_var_redeclaration_without_initializer_preserves_existing_value():
    source = program('var flag=1;var flag;if(flag){value.splice(0,1)};value.reverse()', helpers='')
    assert apply_signature(compile_signature(source), 'abcdef') == 'fedcb'


def test_var_parameter_redeclaration_does_not_clear_input():
    source = 'decode=function(value){var value;value=value.split("");value.reverse();return value.join("")};'
    assert apply_signature(compile_signature(source), 'abc') == 'cba'


@pytest.mark.parametrize('primitive', ['true', 'false', 'null', 'undefined', '2.5', '0/0'])
def test_primitive_constant_is_not_a_symbolic_signature(primitive):
    source = HELPERS + '''route=function(mode,text){mode=mode.split("");
        toolkit.back(mode);return mode.join("")};''' + f'route({primitive},42);'
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


@pytest.mark.parametrize('operator', ['*=', '/=', '%=', '^=', '&=', '|=', '<<=', '>>=', '>>>=', '**='])
def test_all_candidate_compound_rebindings_rejected(operator):
    with pytest.raises(UnsupportedChallenge):
        compile_signature(program('toolkit.back(value)') + f'decode{operator}2;')


@pytest.mark.parametrize('terminator', ['\r', '\u2028', '\u2029'])
def test_all_js_line_terminators_end_comments(terminator):
    source = HELPERS + '//comment' + terminator + 'toolkit.back=other;\n'
    source += program('toolkit.back(value)', helpers='')
    with pytest.raises(UnsupportedChallenge):
        compile_signature(source)


def test_left_deep_ast_is_reported_as_unsupported():
    helpers = 'var toolkit={cut:function(a,b){a.splice(0,' + '1+' * 1100 + '1)}};'
    with pytest.raises(UnsupportedChallenge):
        compile_signature(program('toolkit.cut(value,1)', helpers=helpers))


def test_function_count_budget(monkeypatch):
    monkeypatch.setattr(challenges, 'MAX_FUNCTIONS', 3)
    with pytest.raises(UnsupportedChallenge, match='function limit'):
        compile_signature(program('toolkit.back(value)'))


def test_table_count_budget(monkeypatch):
    monkeypatch.setattr(challenges, 'MAX_TABLES', 1)
    with pytest.raises(UnsupportedChallenge, match='table count'):
        compile_signature('var a="a|b".split("|");var b="c|d".split("|");')


@pytest.mark.parametrize('prefix', [
    'Array.prototype.reverse=function(){return this};',
    'String["prototype"]["split"]=function(){return ["x","y"]};',
    'var proto=Array.prototype;proto.reverse=other;',
    'Object.defineProperty(Array.prototype,"reverse",{value:other});',
    'Array.prototype[unknown]=other;',
    'String=other;',
    'eval("toolkit.back=other");',
    'new Function("toolkit.back=other")();',
])
def test_explicit_intrinsic_or_dynamic_source_mutation_rejected(prefix):
    with pytest.raises(UnsupportedChallenge):
        compile_signature(prefix + program('toolkit.back(value)'))


def test_helper_cannot_read_table_from_unrelated_function_scope():
    helpers = '''function hidden(){var keys="reverse".split("|")};
        var toolkit={back:function(a){a[keys[0]]()}};'''
    with pytest.raises(UnsupportedChallenge):
        compile_signature(program('toolkit.back(value)', helpers=helpers))


def test_parenthesized_prototype_alias_is_rejected():
    prefix = 'var proto=((Array.prototype));proto.reverse=function(){return this};'
    with pytest.raises(UnsupportedChallenge):
        compile_signature(prefix + program('value.reverse()', helpers=''))
