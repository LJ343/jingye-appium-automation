package android.content;
public interface SharedPreferences {
    boolean getBoolean(String key,boolean fallback);
    long getLong(String key,long fallback);
    String getString(String key,String fallback);
    Editor edit();
    interface Editor {
        Editor putBoolean(String key,boolean value);
        Editor putLong(String key,long value);
        Editor putString(String key,String value);
        boolean commit();
    }
}
