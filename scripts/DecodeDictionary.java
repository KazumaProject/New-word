import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.zip.*;

/** Standalone reader for the released v1.7.256 assets; no converter build needed.
 * Format: converter commit 4080e406fa62650061c4e7d3f24daf73cec97dc7,
 * LOUDS.writeExternalNotCompress, LOUDSWithTermId.writeExternalNotCompress,
 * TokenArray.writeExternalNotCompress, and GraphBuilder's -2/-1 outputs.
 */
class DecodeDictionary {
    static final String ROOT = "app/src/main/assets/";
    static final Set<String> PACKS = Set.of("emoji", "emoticon", "english_reading",
        "kotowaza", "neologd", "person_name", "places", "reading_correction",
        "single_kanji", "symbol", "system", "web", "wiki");
    static final int MAX_BYTES = 256 * 1024 * 1024;

    static void require(boolean ok, String reason) throws IOException {
        if (!ok) throw new IOException(reason);
    }

    static byte[] bounded(InputStream stream) throws IOException {
        byte[] raw = stream.readNBytes(MAX_BYTES + 1);
        require(raw.length <= MAX_BYTES, "Oversized dictionary member");
        return raw;
    }

    static ObjectInputStream input(ZipFile zip, String name) throws IOException {
        ZipEntry entry = zip.getEntry(name);
        require(entry != null && !entry.isDirectory(), "Missing dictionary member: " + name);
        byte[] raw;
        try (InputStream stream = zip.getInputStream(entry)) { raw = bounded(stream); }
        if (name.endsWith(".zip")) {
            try (ZipInputStream nested = new ZipInputStream(new ByteArrayInputStream(raw))) {
                ZipEntry inner = nested.getNextEntry();
                require(inner != null && !inner.isDirectory(), "Invalid nested dictionary ZIP");
                raw = bounded(nested);
                require(nested.getNextEntry() == null, "Unexpected nested dictionary member");
            }
        }
        ObjectInputStream stream = new ObjectInputStream(new ByteArrayInputStream(raw));
        stream.setObjectInputFilter(info -> {
            Class<?> type = info.serialClass();
            if (info.depth() > 8 || info.arrayLength() > MAX_BYTES / 2)
                return ObjectInputFilter.Status.REJECTED;
            if (type == null) return ObjectInputFilter.Status.UNDECIDED;
            return type == BitSet.class || type == char[].class || type == int[].class
                || type == short[].class || type == long[].class
                ? ObjectInputFilter.Status.ALLOWED : ObjectInputFilter.Status.REJECTED;
        });
        return stream;
    }

    static void end(ObjectInputStream stream) throws Exception {
        try { stream.readObject(); throw new IOException("Unexpected dictionary object"); }
        catch (EOFException expected) { }
    }

    static final class Trie {
        final BitSet bits, leaves;
        final char[] labels;
        final int[] ranks, ones, terms;

        Trie(ObjectInputStream stream, boolean reading) throws Exception {
            try (stream) {
                bits = (BitSet)stream.readObject();
                leaves = (BitSet)stream.readObject();
                labels = (char[])stream.readObject();
                terms = reading ? (int[])stream.readObject() : null;
                end(stream);
            }
            require(bits.get(0) && !bits.get(1) && labels.length == bits.cardinality() + 1,
                "Invalid LOUDS shape");
            require(!leaves.get(0) && !leaves.get(1), "Invalid root terminal");
            BitSet badLeaves = (BitSet)leaves.clone();
            badLeaves.andNot(bits);
            require(badLeaves.isEmpty(), "Terminal on a LOUDS zero");
            require(!reading || terms.length == leaves.cardinality(), "Incomplete reading term IDs");
            ranks = new int[bits.length()];
            ones = new int[bits.cardinality()];
            int count = 0;
            for (int pos = 0; pos < ranks.length; pos++) {
                if (bits.get(pos)) ones[count++] = pos;
                ranks[pos] = count;
            }
            for (int pos : ones) {
                if (pos == 0) continue;
                int parentRank = pos + 1 - ranks[pos];
                require(parentRank > 0 && parentRank <= ones.length && ones[parentRank - 1] < pos,
                    "Invalid LOUDS parent");
            }
        }

        String word(int node) throws IOException {
            require(node >= 2 && node < ranks.length && bits.get(node) && leaves.get(node),
                "Invalid conversion terminal");
            StringBuilder reversed = new StringBuilder();
            while (node != 0) {
                reversed.append(labels[ranks[node]]);
                node = ones[node - ranks[node]];
            }
            // Reverse UTF-16 code units exactly as LOUDS.getLetter does.
            // StringBuilder.reverse preserves surrogate pairs, which would
            // join the wrong halves when two emoji are adjacent in this buffer.
            char[] value = new char[reversed.length()];
            for (int i = 0; i < value.length; i++) value[i] = reversed.charAt(value.length - 1 - i);
            return new String(value);
        }
    }

    record Tokens(int[] nodes, int[] boundaries) { }

    static Tokens tokens(ObjectInputStream stream) throws Exception {
        short[] pos, costs;
        int[] nodes;
        BitSet bits;
        try (stream) {
            pos = (short[])stream.readObject();
            costs = (short[])stream.readObject();
            nodes = (int[])stream.readObject();
            bits = (BitSet)stream.readObject();
            end(stream);
        }
        require(nodes.length > 0 && pos.length == nodes.length && costs.length == nodes.length
            && bits.cardinality() == nodes.length && !bits.get(0), "Invalid token arrays");
        // A trailing zero was not explicitly stored in legacy BitSets. Its
        // position follows the last posting, not BitSet's padded capacity.
        int[] boundaries = new int[bits.length() + 1 - bits.cardinality()];
        int ones = 0, zero = 0;
        for (int p = 0; p <= bits.length(); p++) {
            if (bits.get(p)) ones++;
            else boundaries[zero++] = ones;
        }
        require(ones == nodes.length && zero == boundaries.length, "Incomplete token boundaries");
        return new Tokens(nodes, boundaries);
    }

    static String katakana(String reading) {
        StringBuilder value = new StringBuilder();
        for (char ch : reading.toCharArray())
            value.append(ch >= '\u3041' && ch <= '\u3096' ? (char)(ch + 0x60) : ch);
        return value.toString();
    }

    static String encoded(String value) {
        return Base64.getEncoder().encodeToString(value.getBytes(StandardCharsets.UTF_8));
    }

    public static void main(String[] args) throws Exception {
        require(args.length == 3, "Usage: DecodeDictionary.java asset.zip rows.tsv report.json");
        Map<String, Long> counts = new TreeMap<>();
        try (ZipFile zip = new ZipFile(args[0]);
             BufferedWriter out = Files.newBufferedWriter(Path.of(args[1]), StandardCharsets.UTF_8)) {
            List<String> all = zip.stream().map(ZipEntry::getName).toList();
            require(new HashSet<>(all).size() == all.size(), "Duplicate dictionary ZIP members");
            List<String> paths = all.stream().filter(name -> name.startsWith(ROOT)
                && name.substring(name.lastIndexOf('/') + 1).startsWith("yomi")
                && !name.endsWith("/")).sorted().toList();
            require(paths.size() == PACKS.size(), "Incomplete dictionary pack coverage");
            Set<String> consumed = new HashSet<>();
            for (String path : paths) {
                int slash = path.lastIndexOf('/') + 1;
                String prefix = path.substring(0, slash), suffix = path.substring(slash + 4);
                String pack = prefix.substring(ROOT.length(), prefix.length() - 1);
                require(PACKS.contains(pack) && !counts.containsKey(pack), "Unexpected dictionary pack");
                Trie reading = new Trie(input(zip, path), true);
                Trie written = new Trie(input(zip, prefix + "tango" + suffix), false);
                Tokens tokens = tokens(input(zip, prefix + "token" + suffix));
                consumed.addAll(List.of(path, prefix + "tango" + suffix, prefix + "token" + suffix));
                BitSet covered = new BitSet(tokens.nodes.length);
                Set<Integer> termIds = new HashSet<>();
                int termIndex = 0;
                long decoded = 0;
                for (int node = reading.leaves.nextSetBit(0); node >= 0; node = reading.leaves.nextSetBit(node + 1)) {
                    int term = reading.terms[termIndex++];
                    require(term >= 1 && term < tokens.boundaries.length && termIds.add(term), "Invalid reading term ID");
                    String yomi = reading.word(node);
                    require(!yomi.isBlank(), "Empty dictionary reading");
                    int start = tokens.boundaries[term - 1], stop = tokens.boundaries[term];
                    int prior = covered.nextSetBit(start);
                    require(prior < 0 || prior >= stop, "Duplicate token coverage");
                    covered.set(start, stop);
                    for (int i = start; i < stop; i++) {
                        int wordNode = tokens.nodes[i];
                        String word = switch (wordNode) {
                            case -2 -> yomi;
                            case -1 -> katakana(yomi);
                            default -> written.word(wordNode);
                        };
                        if (pack.equals("reading_correction")) word = word.split("\t", 2)[0];
                        require(!word.isBlank(), "Empty dictionary conversion output");
                        // Internal transport uses base64 so tabs/newlines in a
                        // conversion output cannot silently split index rows.
                        out.write(pack + "\t" + encoded(yomi) + "\t" + encoded(word) + "\n");
                        decoded++;
                    }
                }
                require(decoded == tokens.nodes.length && covered.cardinality() == tokens.nodes.length,
                    "Incomplete decoding: " + pack);
                counts.put(pack, decoded);
            }
            require(counts.keySet().equals(PACKS), "Incomplete dictionary pack coverage");
            for (String name : all) {
                String base = name.substring(name.lastIndexOf('/') + 1);
                if (name.startsWith(ROOT) && (base.startsWith("yomi") || base.startsWith("tango") || base.startsWith("token")))
                    require(consumed.contains(name), "Unchecked dictionary word asset: " + name);
            }
        }
        List<String> fields = new ArrayList<>();
        for (var item : counts.entrySet()) fields.add("\"" + item.getKey() + "\":" + item.getValue());
        Files.writeString(Path.of(args[2]), "{\"packs\":{" + String.join(",", fields) + "}}\n", StandardCharsets.UTF_8);
    }
}
