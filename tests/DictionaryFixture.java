import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.zip.*;

/** Small real serialized assets exercise the standalone reader without networking. */
class DictionaryFixture {
    static class Node {
        final Map<Character, Node> children = new TreeMap<>();
        boolean terminal;
        int position;
    }
    record Trie(BitSet bits, BitSet leaves, char[] labels, Map<String, Integer> positions) { }

    static Trie trie(String... words) {
        Node root = new Node();
        Map<String, Node> terminals = new HashMap<>();
        for (String word : words) {
            Node node = root;
            for (char ch : word.toCharArray()) node = node.children.computeIfAbsent(ch, c -> new Node());
            node.terminal = true;
            terminals.put(word, node);
        }
        BitSet bits = new BitSet(), leaves = new BitSet();
        bits.set(0);
        List<Character> labels = new ArrayList<>(List.of(' ', ' '));
        Queue<Node> queue = new ArrayDeque<>(List.of(root));
        int pos = 2;
        while (!queue.isEmpty()) {
            Node node = queue.remove();
            for (var child : node.children.entrySet()) {
                bits.set(pos);
                child.getValue().position = pos;
                if (child.getValue().terminal) leaves.set(pos);
                labels.add(child.getKey());
                queue.add(child.getValue());
                pos++;
            }
            pos++;
        }
        char[] chars = new char[labels.size()];
        for (int i = 0; i < chars.length; i++) chars[i] = labels.get(i);
        Map<String, Integer> positions = new HashMap<>();
        terminals.forEach((word, node) -> positions.put(word, node.position));
        return new Trie(bits, leaves, chars, positions);
    }

    static void member(ZipOutputStream zip, String name, Object... objects) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (ObjectOutputStream out = new ObjectOutputStream(bytes)) {
            for (Object object : objects) out.writeObject(object);
        }
        byte[] raw = bytes.toByteArray();
        if (name.endsWith(".zip")) {
            bytes = new ByteArrayOutputStream();
            try (ZipOutputStream inner = new ZipOutputStream(bytes)) {
                inner.putNextEntry(new ZipEntry("asset.dat"));
                inner.write(raw);
                inner.closeEntry();
            }
            raw = bytes.toByteArray();
        }
        zip.putNextEntry(new ZipEntry(name));
        zip.write(raw);
        zip.closeEntry();
    }

    public static void main(String[] args) throws Exception {
        String mode = args.length > 1 ? args[1] : "valid";
        Map<String, String> packs = Map.ofEntries(
            Map.entry("emoji", "_emoji.dat"), Map.entry("emoticon", "_emoticon.dat"),
            Map.entry("english_reading", ".dat.zip"), Map.entry("kotowaza", "_kotowaza.dat"),
            Map.entry("neologd", "_neologd.dat.zip"), Map.entry("person_name", "_person_names.dat"),
            Map.entry("places", "_places.dat.zip"), Map.entry("reading_correction", "_reading_correction.dat"),
            Map.entry("single_kanji", "_singleKanji.dat"), Map.entry("symbol", "_symbol.dat"),
            Map.entry("system", ".dat.zip"), Map.entry("web", "_web.dat.zip"), Map.entry("wiki", "_wiki.dat.zip"));
        try (ZipOutputStream zip = new ZipOutputStream(Files.newOutputStream(Path.of(args[0])))) {
            for (var pack : packs.entrySet()) {
                if (mode.equals("missing") && pack.getKey().equals("places")) continue;
                String last = pack.getKey().equals("reading_correction") ? "補正語\t説明" : "Ｃｌｏｕｄ　Ｎｏｖａ";
                if (pack.getKey().equals("emoji")) last = "🇦🇨";
                Trie yomi = trie("かな"), tango = trie("東京", "ヨーグルト", last);
                int[] nodes = {-2, -1, tango.positions.get("東京"), tango.positions.get("ヨーグルト"), tango.positions.get(last)};
                if (mode.equals("sentinel")) nodes[0] = -3;
                if (mode.equals("boundary")) {
                    nodes = new int[63];
                    Arrays.fill(nodes, tango.positions.get("東京"));
                }
                BitSet postings = new BitSet();
                postings.set(1, nodes.length + 1);
                if (mode.equals("postings")) postings.clear(1);
                String prefix = "app/src/main/assets/" + pack.getKey() + "/";
                member(zip, prefix + "yomi" + pack.getValue(), yomi.bits, yomi.leaves, yomi.labels, new int[]{1});
                member(zip, prefix + "tango" + pack.getValue(), tango.bits, tango.leaves, tango.labels);
                member(zip, prefix + "token" + pack.getValue(), new short[nodes.length], new short[nodes.length], nodes, postings);
            }
        }
    }
}
