// Synthetic input. It compiles with JDK 21.
class YieldRepro {
    static long apply(int kind, long bits) {
        return switch (kind) {
            case 1 -> { yield bits ^ 1L; }
            default -> 0L;
        };
    }
}
