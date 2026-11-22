import java.util.function.IntUnaryOperator;

enum EnumLambdaRepro {
    VALUE(value -> value + 1);

    private final IntUnaryOperator operation;

    EnumLambdaRepro(IntUnaryOperator operation) {
        this.operation = operation;
    }

    int apply(int value) {
        return operation.applyAsInt(value);
    }
}
