import struct

import pytest

from eval.java_bytecode_probe import class_manifest, fixture_quality_gates, graph_evidence, instruction_evidence


def test_class_manifest_requires_java_21_and_real_output(tmp_path):
    with pytest.raises(ValueError, match="did not produce"):
        class_manifest(tmp_path)
    path = tmp_path / "Fixture.class"
    path.write_bytes(struct.pack(">IHH", 0xCAFEBABE, 0, 61))
    with pytest.raises(ValueError, match="major=61"):
        class_manifest(tmp_path)
    path.write_bytes(struct.pack(">IHH", 0xCAFEBABE, 0, 65))
    assert class_manifest(tmp_path)[0]["major"] == 65


def test_instruction_evidence_requires_opcodes():
    assert not instruction_evidence("A method named lxor is not an opcode")["long_xor_instruction"]
    result = instruction_evidence("22: lxor\nprivate static int lambda$static$0(int);\n2: iadd\n")
    assert result == {"long_xor_instruction": True, "integer_add_instruction": True,
                      "lambda_method_names": ["lambda$static$0"]}


def test_operator_evidence_uses_ast_owner(tmp_path):
    dot = tmp_path / "export.dot"
    dot.write_text('''digraph {
"1" [label="METHOD" NAME="other" FULL_NAME="Fixture.other:void()"];
"2" [label="CALL" NAME="<operator>.xor" CODE="bits ^ 1L"];
"3" [label="CALL" NAME="<operator>.addition" CODE="value + 1"];
"1" -> "2" [label="AST"];
"1" -> "3" [label="AST"];
}''')
    result = graph_evidence(dot)
    assert len(result["operator_and_lambda_calls"]) == 2
    assert result["xor_in_apply"] is False
    assert result["addition_in_lambda_body"] is False


def test_missing_operation_fails_the_fixture_gate():
    counts = {"bytecode_evidence": {"long_xor_instruction": True},
              "operation_evidence": {"xor_in_apply": False}}
    assert fixture_quality_gates("yield", counts)["status"] == "failed"
    assert fixture_quality_gates("yield", None)["status"] == "not_evaluated"


def test_lambda_gate_requires_the_javac_method_owner():
    counts = {
        "bytecode_evidence": {"integer_add_instruction": True, "lambda_method_names": ["lambda$static$0"]},
        "operation_evidence": {"operator_and_lambda_calls": [
            {"name": "<operator>.addition", "owner_name": "lambda$other$1"},
        ]},
    }
    assert fixture_quality_gates("enum_lambda", counts)["status"] == "failed"
    counts["operation_evidence"]["operator_and_lambda_calls"][0]["owner_name"] = "lambda$static$0"
    assert fixture_quality_gates("enum_lambda", counts)["status"] == "passed"
